from collections import OrderedDict
from logging import getLogger
from typing import Dict, Generator, Iterable, List, Optional, Sequence, Set

from django.conf import settings
from django.db import transaction

from eth_typing import HexStr
from hexbytes import HexBytes
from web3.types import ParityBlockTrace, ParityFilterTrace

from gnosis.eth import EthereumClient

from safe_transaction_service.contracts.tx_decoder import (
    CannotDecode,
    get_safe_tx_decoder,
)
from safe_transaction_service.utils.utils import FixedSizeDict, chunks

from ..models import InternalTx, InternalTxDecoded, MonitoredAddress, SafeMasterCopy
from .ethereum_indexer import EthereumIndexer, FindRelevantElementsException

logger = getLogger(__name__)


class InternalTxIndexerProvider:
    def __new__(cls):
        if not hasattr(cls, "instance"):
            from django.conf import settings

            if settings.ETH_INTERNAL_NO_FILTER:
                instance_class = InternalTxIndexerWithTraceBlock
            else:
                instance_class = InternalTxIndexer

            cls.instance = instance_class(
                EthereumClient(settings.ETHEREUM_TRACING_NODE_URL),
            )
        return cls.instance

    @classmethod
    def del_singleton(cls):
        if hasattr(cls, "instance"):
            del cls.instance


class InternalTxIndexer(EthereumIndexer):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault(
            "block_process_limit", settings.ETH_INTERNAL_TXS_BLOCK_PROCESS_LIMIT
        )
        kwargs.setdefault("blocks_to_reindex_again", 6)
        super().__init__(*args, **kwargs)

        self.trace_txs_batch_size: int = settings.ETH_INTERNAL_TRACE_TXS_BATCH_SIZE
        self.number_trace_blocks: int = (
            10  # Use `trace_block` for last `number_trace_blocks` blocks indexing
        )
        self.tx_decoder = get_safe_tx_decoder()
        self._processed_element_cache = FixedSizeDict(maxlen=40_000)  # Around 3MiB

    def mark_as_processed(
        self, tx_hash: HexBytes, block_hash: Optional[HexBytes]
    ) -> bool:
        """
        Mark a `tx_hash` as processed by the indexer

        :param tx_hash:
        :param block_hash:
        :return: `True` if `tx_hash` was marked as processed, `False` if it was already processed
        """

        tx_hash = HexBytes(tx_hash)
        block_hash = HexBytes(block_hash or 0)
        tx_id = tx_hash + block_hash

        if tx_id in self._processed_element_cache:
            logger.debug(
                "Tx with tx-hash=%s on block=%s was already processed",
                tx_hash.hex(),
                block_hash.hex(),
            )
            return False
        else:
            logger.debug(
                "Marking tx with tx-hash=%s on block=%s as processed",
                tx_hash.hex(),
                block_hash.hex(),
            )
            self._processed_element_cache[tx_id] = None
            return True

    @property
    def database_field(self):
        return "tx_block_number"

    @property
    def database_queryset(self):
        return SafeMasterCopy.objects.all()

    def find_relevant_elements(
        self,
        addresses: Sequence[str],
        from_block_number: int,
        to_block_number: int,
        current_block_number: Optional[int] = None,
    ) -> OrderedDict[HexStr, Optional[ParityFilterTrace]]:
        current_block_number = (
            current_block_number or self.ethereum_client.current_block_number
        )
        # Use `trace_block` for last `number_trace_blocks` blocks and `trace_filter` for the others
        trace_block_number = max(current_block_number - self.number_trace_blocks, 0)
        if from_block_number > trace_block_number:  # Just trace_block
            logger.debug(
                "Using trace_block from-block=%d to-block=%d",
                from_block_number,
                to_block_number,
            )
            return self._find_relevant_elements_using_trace_block(
                addresses, from_block_number, to_block_number
            )
        elif to_block_number < trace_block_number:  # Just trace_filter
            logger.debug(
                "Using trace_filter from-block=%d to-block=%d",
                from_block_number,
                to_block_number,
            )
            return self._find_relevant_elements_using_trace_filter(
                addresses, from_block_number, to_block_number
            )
        else:  # trace_filter for old blocks and trace_filter for the most recent ones
            logger.debug(
                "Using trace_filter from-block=%d to-block=%d and trace_block from-block=%d to-block=%d",
                from_block_number,
                trace_block_number,
                trace_block_number,
                to_block_number,
            )
            relevant_elements = self._find_relevant_elements_using_trace_filter(
                addresses, from_block_number, trace_block_number
            )
            relevant_elements.update(
                self._find_relevant_elements_using_trace_block(
                    addresses, trace_block_number, to_block_number
                )
            )
            return relevant_elements

    def _find_relevant_elements_using_trace_block(
        self, addresses: Sequence[str], from_block_number: int, to_block_number: int
    ) -> OrderedDict[HexStr, ParityFilterTrace]:
        addresses_set = set(addresses)  # More optimal to use with `in`
        try:
            block_numbers = list(range(from_block_number, to_block_number + 1))

            with self.auto_adjust_block_limit(from_block_number, to_block_number):
                blocks_traces: ParityBlockTrace = (
                    self.ethereum_client.parity.trace_blocks(block_numbers)
                )
            traces: OrderedDict[HexStr, ParityFilterTrace] = OrderedDict()
            relevant_tx_hashes: Set[HexStr] = set()
            for block_number, block_traces in zip(block_numbers, blocks_traces):
                if not block_traces:
                    logger.warning("Empty `trace_block` for block=%d", block_number)

                for trace in block_traces:
                    transaction_hash = trace.get("transactionHash")
                    if transaction_hash:
                        traces.setdefault(transaction_hash, []).append(trace)
                        # We're only interested in traces related to the provided addresses
                        if (
                            trace.get("action", {}).get("from") in addresses_set
                            or trace.get("action", {}).get("to") in addresses_set
                        ):
                            relevant_tx_hashes.add(transaction_hash)

            # Remove not relevant traces
            for tx_hash in list(traces.keys()):
                if tx_hash not in relevant_tx_hashes:
                    del traces[tx_hash]

            return traces
        except IOError as e:
            raise FindRelevantElementsException(
                "Request error calling `trace_block`"
            ) from e

    def _find_relevant_elements_using_trace_filter(
        self, addresses: Sequence[str], from_block_number: int, to_block_number: int
    ) -> OrderedDict[HexStr, List]:
        """
        Search for tx hashes with internal txs (in and out) of a `address`

        :param addresses:
        :param from_block_number: Starting block number
        :param to_block_number: Ending block number
        :return: Tx hashes of txs with internal txs relevant for the `addresses`
        """
        logger.debug(
            "Searching for internal txs from block-number=%d to block-number=%d - Addresses=%s",
            from_block_number,
            to_block_number,
            addresses,
        )

        try:
            # We only need to search for traces `to` the provided addresses
            with self.auto_adjust_block_limit(from_block_number, to_block_number):
                to_traces = self.ethereum_client.parity.trace_filter(
                    from_block=from_block_number,
                    to_block=to_block_number,
                    to_address=addresses,
                )
        except IOError as e:
            raise FindRelevantElementsException(
                "Request error calling `trace_filter`"
            ) from e

        # Log INFO if traces found, DEBUG if not
        traces: OrderedDict[HexStr, None] = OrderedDict()
        for trace in to_traces:
            transaction_hash = trace.get("transactionHash")
            if transaction_hash:
                # Leave this empty, as we are missing traces for the transaction and will need to be fetched later
                traces[transaction_hash] = []

        log_fn = logger.info if traces else logger.debug
        log_fn(
            "Found %d relevant txs with internal txs between block-number=%d and block-number=%d. Addresses=%s",
            len(traces),
            from_block_number,
            to_block_number,
            addresses,
        )

        return traces

    def _get_internal_txs_to_decode(
        self, tx_hashes: Sequence[str]
    ) -> Generator[InternalTxDecoded, None, None]:
        """
        Use generator to be more RAM friendly
        """
        for internal_tx in InternalTx.objects.can_be_decoded().filter(
            ethereum_tx__in=tx_hashes
        ):
            try:
                function_name, arguments = self.tx_decoder.decode_transaction(
                    bytes(internal_tx.data)
                )
                if (
                    internal_tx.pk is None
                ):  # pk is not populated on `bulk_create ignore_conflicts=True`
                    internal_tx = InternalTx.objects.get(
                        ethereum_tx=internal_tx.ethereum_tx,
                        trace_address=internal_tx.trace_address,
                    )
                yield InternalTxDecoded(
                    internal_tx=internal_tx,
                    function_name=function_name,
                    arguments=arguments,
                    processed=False,
                )
            except CannotDecode:
                pass

    def trace_transactions(
        self, tx_hashes: Sequence[HexStr], batch_size: int
    ) -> Iterable[List[ParityFilterTrace]]:
        batch_size = batch_size or len(tx_hashes)  # If `0`, don't use batches
        for tx_hash_chunk in chunks(list(tx_hashes), batch_size):
            tx_hash_chunk = list(tx_hash_chunk)
            try:
                yield from self.ethereum_client.parity.trace_transactions(tx_hash_chunk)
            except IOError:
                logger.error(
                    "Problem calling `trace_transactions` with %d txs. "
                    "Try lowering ETH_INTERNAL_TRACE_TXS_BATCH_SIZE",
                    len(tx_hash_chunk),
                    exc_info=True,
                )
                raise

    def process_elements(
        self, tx_hash_with_traces: OrderedDict[HexStr, Optional[ParityFilterTrace]]
    ) -> List[InternalTx]:
        """
        :param tx_hash_with_traces:
        :return: Inserted `InternalTx` objects
        """
        # Prefetch ethereum txs
        if not tx_hash_with_traces:
            return []

        # Copy as we might modify it
        tx_hash_with_traces = dict(tx_hash_with_traces)

        logger.debug(
            "Prefetching and storing %d ethereum txs", len(tx_hash_with_traces)
        )

        tx_hashes = []
        tx_hashes_missing_traces = []
        for tx_hash in list(tx_hash_with_traces.keys()):
            # Check if transactions have already been processed
            # Provide block_hash if available as a mean to prevent reorgs
            block_hash = (
                tx_hash_with_traces[tx_hash][0]["blockHash"]
                if tx_hash_with_traces[tx_hash]
                else None
            )
            if self.mark_as_processed(tx_hash, block_hash):
                tx_hashes.append(tx_hash)
                # Traces can be already populated if using `trace_block`, but with `trace_filter`
                # some traces will be missing and `trace_transaction` needs to be called
                if not tx_hash_with_traces[tx_hash]:
                    tx_hashes_missing_traces.append(tx_hash)
            else:
                del tx_hash_with_traces[tx_hash]

        ethereum_txs = self.index_service.txs_create_or_update_from_tx_hashes(tx_hashes)
        logger.debug("End prefetching and storing of ethereum txs")

        logger.debug("Prefetching of traces(internal txs)")
        missing_traces_lists = self.trace_transactions(
            tx_hashes_missing_traces, batch_size=self.trace_txs_batch_size
        )
        for tx_hash_missing_traces, missing_traces in zip(
            tx_hashes_missing_traces, missing_traces_lists
        ):
            tx_hash_with_traces[tx_hash_missing_traces] = missing_traces

        internal_txs = (
            InternalTx.objects.build_from_trace(trace, ethereum_tx)
            for ethereum_tx in ethereum_txs
            for trace in self.ethereum_client.parity.filter_out_errored_traces(
                tx_hash_with_traces[ethereum_tx.tx_hash]
            )
        )

        revelant_internal_txs_batch = (
            trace for trace in internal_txs if trace.is_relevant
        )
        logger.debug("End prefetching of traces(internal txs)")

        logger.debug("Storing traces")
        with transaction.atomic():
            traces_stored = InternalTx.objects.bulk_create_from_generator(
                revelant_internal_txs_batch, ignore_conflicts=True
            )
            logger.debug("End storing of %d traces", traces_stored)

            logger.debug("Start decoding and storing of decoded traces")
            internal_txs_decoded = InternalTxDecoded.objects.bulk_create_from_generator(
                self._get_internal_txs_to_decode(tx_hashes), ignore_conflicts=True
            )
            logger.debug(
                "End decoding and storing of %d decoded traces", internal_txs_decoded
            )
            return tx_hashes


class InternalTxIndexerWithTraceBlock(InternalTxIndexer):
    """
    Indexer for nodes not supporting `trace_filter`, so it will always use `trace_block`
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def get_almost_updated_addresses(
        self, current_block_number: int
    ) -> List[MonitoredAddress]:
        """
        Return every address. As we are using `trace_block` every master copy should be processed together

        :param current_block_number:
        :return:
        """
        return self.get_not_updated_addresses(current_block_number)

    def find_relevant_elements(
        self,
        addresses: Sequence[str],
        from_block_number: int,
        to_block_number: int,
        current_block_number: Optional[int] = None,
    ) -> Dict[HexStr, ParityFilterTrace]:
        logger.debug(
            "Using trace_block from-block=%d to-block=%d",
            from_block_number,
            to_block_number,
        )
        return self._find_relevant_elements_using_trace_block(
            addresses, from_block_number, to_block_number
        )
