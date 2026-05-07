"""Tests for routers/utils/bulk.py."""

import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from gumnut import NotFoundError

from routers.immich_models import BulkIdErrorReason, Error1
from routers.utils.bulk import (
    BulkChunkError,
    BulkChunkOutcome,
    BulkChunkSuccess,
    chunked_per_item_bulk,
    classify_bulk_item_call,
)
from routers.utils.gumnut_client import BULK_CHUNK_SIZE
from routers.utils.gumnut_id_conversion import uuid_to_gumnut_asset_id
from tests.conftest import make_sdk_connection_error, make_sdk_status_error


async def _collect(asyncgen) -> list[BulkChunkOutcome]:
    return [outcome async for outcome in asyncgen]


class TestChunkedPerItemBulk:
    @pytest.mark.anyio
    async def test_empty_input_yields_nothing(self):
        sdk_call = AsyncMock()
        outcomes = await _collect(
            chunked_per_item_bulk(
                [],
                sdk_call,
                log_context="test",
                log_extra={},
            )
        )
        assert outcomes == []
        sdk_call.assert_not_called()

    @pytest.mark.anyio
    async def test_single_chunk_success_passes_response_through(self):
        asset_uuids = [uuid4(), uuid4()]
        gumnut_ids = [uuid_to_gumnut_asset_id(u) for u in asset_uuids]
        sdk_call = AsyncMock(return_value="response-payload")

        outcomes = await _collect(
            chunked_per_item_bulk(
                asset_uuids,
                sdk_call,
                log_context="test",
                log_extra={"album_id": "abc"},
            )
        )

        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert isinstance(outcome, BulkChunkSuccess)
        assert outcome.chunk_uuids == tuple(asset_uuids)
        assert outcome.response == "response-payload"
        sdk_call.assert_called_once_with(gumnut_ids)

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "total, expected_chunks",
        [
            # Exact-boundary cases: pinning these locks the chunking math
            # against a future hand-rolled `if len(ids) > N` style split.
            (BULK_CHUNK_SIZE, 1),
            (BULK_CHUNK_SIZE + 1, 2),
            (BULK_CHUNK_SIZE * 2 + 5, 3),
        ],
    )
    async def test_splits_oversized_input_into_ordered_chunks(
        self, total, expected_chunks
    ):
        asset_uuids = [uuid4() for _ in range(total)]
        gumnut_ids = [uuid_to_gumnut_asset_id(u) for u in asset_uuids]
        sdk_call = AsyncMock(side_effect=[f"r{i}" for i in range(expected_chunks)])

        outcomes = await _collect(
            chunked_per_item_bulk(
                asset_uuids,
                sdk_call,
                log_context="test",
                log_extra={},
            )
        )

        assert len(outcomes) == expected_chunks
        assert sdk_call.call_count == expected_chunks
        for idx, outcome in enumerate(outcomes):
            expected_slice = slice(idx * BULK_CHUNK_SIZE, (idx + 1) * BULK_CHUNK_SIZE)
            assert isinstance(outcome, BulkChunkSuccess)
            assert outcome.chunk_uuids == tuple(asset_uuids[expected_slice])
            assert outcome.response == f"r{idx}"
            # Each chunk's SDK call receives only its slice's gumnut ids.
            assert sdk_call.call_args_list[idx].args == (gumnut_ids[expected_slice],)

    @pytest.mark.anyio
    async def test_api_status_error_classified_as_error1(self):
        asset_uuids = [uuid4(), uuid4()]
        sdk_call = AsyncMock(
            side_effect=make_sdk_status_error(404, "Not found", cls=NotFoundError)
        )

        outcomes = await _collect(
            chunked_per_item_bulk(
                asset_uuids,
                sdk_call,
                log_context="test",
                log_extra={},
            )
        )

        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert isinstance(outcome, BulkChunkError)
        assert outcome.error == Error1.not_found

    @pytest.mark.anyio
    async def test_unrecognized_status_error_falls_back_to_unknown(self):
        asset_uuids = [uuid4()]
        sdk_call = AsyncMock(side_effect=make_sdk_status_error(500, "boom"))

        outcomes = await _collect(
            chunked_per_item_bulk(
                asset_uuids,
                sdk_call,
                log_context="test",
                log_extra={},
            )
        )

        outcome = outcomes[0]
        assert isinstance(outcome, BulkChunkError)
        assert outcome.error == Error1.unknown

    @pytest.mark.anyio
    async def test_transport_error_logs_with_chunk_and_request_size(self, caplog):
        total = BULK_CHUNK_SIZE + 3
        asset_uuids = [uuid4() for _ in range(total)]
        sdk_call = AsyncMock(
            side_effect=[
                make_sdk_connection_error(),
                make_sdk_connection_error(),
            ]
        )

        with caplog.at_level(logging.WARNING):
            outcomes = await _collect(
                chunked_per_item_bulk(
                    asset_uuids,
                    sdk_call,
                    log_context="test_ctx",
                    log_extra={"album_id": "alb-1"},
                )
            )

        assert all(
            isinstance(o, BulkChunkError) and o.error == Error1.unknown
            for o in outcomes
        )
        # First chunk's log record carries chunk_size=BULK_CHUNK_SIZE and the
        # full request_size; trailing-chunk record carries the residual size.
        records = [
            r for r in caplog.records if r.message == "Transport error in test_ctx"
        ]
        assert len(records) == 2
        assert records[0].chunk_size == BULK_CHUNK_SIZE
        assert records[0].request_size == total
        assert records[0].album_id == "alb-1"
        assert records[1].chunk_size == 3
        assert records[1].request_size == total

    @pytest.mark.anyio
    async def test_mixed_success_and_failure_chunks_yield_in_order(self):
        total = BULK_CHUNK_SIZE * 3
        asset_uuids = [uuid4() for _ in range(total)]
        sdk_call = AsyncMock(
            side_effect=[
                "ok-0",
                make_sdk_status_error(500, "boom"),
                "ok-2",
            ]
        )

        outcomes = await _collect(
            chunked_per_item_bulk(
                asset_uuids,
                sdk_call,
                log_context="test",
                log_extra={},
            )
        )

        assert isinstance(outcomes[0], BulkChunkSuccess)
        assert outcomes[0].response == "ok-0"
        assert isinstance(outcomes[1], BulkChunkError)
        assert outcomes[1].error == Error1.unknown
        assert isinstance(outcomes[2], BulkChunkSuccess)
        assert outcomes[2].response == "ok-2"


class TestClassifyBulkItemCall:
    @pytest.mark.anyio
    async def test_success_returns_none_and_awaits_coroutine(self):
        sdk_call = AsyncMock(return_value="resp")
        result = await classify_bulk_item_call(
            sdk_call(),
            error_enum=Error1,
            log_context="test",
            log_extra={},
        )
        assert result is None
        sdk_call.assert_awaited_once()

    @pytest.mark.anyio
    async def test_api_status_error_classified(self):
        async def raises_not_found():
            raise make_sdk_status_error(404, "Not found", cls=NotFoundError)

        result = await classify_bulk_item_call(
            raises_not_found(),
            error_enum=Error1,
            log_context="test",
            log_extra={},
        )
        assert result == Error1.not_found

    @pytest.mark.anyio
    async def test_unrecognized_status_falls_back_to_unknown(self):
        async def raises_500():
            raise make_sdk_status_error(500, "boom")

        result = await classify_bulk_item_call(
            raises_500(),
            error_enum=Error1,
            log_context="test",
            log_extra={},
        )
        assert result == Error1.unknown

    @pytest.mark.anyio
    async def test_transport_error_logs_and_returns_unknown(self, caplog):
        async def raises_transport():
            raise make_sdk_connection_error()

        with caplog.at_level(logging.WARNING):
            result = await classify_bulk_item_call(
                raises_transport(),
                error_enum=Error1,
                log_context="test_ctx",
                log_extra={"person_id": "p-1"},
            )

        assert result == Error1.unknown
        records = [
            r for r in caplog.records if r.message == "Transport error in test_ctx"
        ]
        assert len(records) == 1
        assert records[0].person_id == "p-1"

    @pytest.mark.anyio
    async def test_works_with_alternate_error_enum(self):
        """Helper is generic over the per-item error enum (Error1 vs BulkIdErrorReason)."""

        async def raises_not_found():
            raise make_sdk_status_error(404, "Not found", cls=NotFoundError)

        result = await classify_bulk_item_call(
            raises_not_found(),
            error_enum=BulkIdErrorReason,
            log_context="test",
            log_extra={},
        )
        assert result == BulkIdErrorReason.not_found
