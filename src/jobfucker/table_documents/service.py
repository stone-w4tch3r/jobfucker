"""Reusable dump, decode, plan, and apply orchestration for table documents."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import JsonValue
from rusty_results.prelude import Err, Ok, Result

from .codec import DocumentFormat, decode_document, encode_document


@dataclass(frozen=True, slots=True)
class TableDocumentDumpOutput:
    """One encoded table document and its visible row count."""

    content: bytes
    row_count: int


class TableDocumentPolicy[DocumentT, PlanT, SummaryT](Protocol):
    """Typed table-specific hooks consumed by the reusable orchestrator."""

    @property
    def schema(self) -> Mapping[str, JsonValue]: ...

    async def dump(self) -> Result[DocumentT, str]: ...

    def to_json_document(
        self,
        document: DocumentT,
    ) -> Result[dict[str, JsonValue], str]: ...  # lint-ignore[raw-dict]: validated document boundary

    def from_json_document(self, document: Mapping[str, JsonValue]) -> Result[DocumentT, str]: ...

    def row_count(self, document: DocumentT) -> int: ...

    async def plan(self, document: DocumentT) -> Result[PlanT, str]: ...

    async def apply(self, plan: PlanT) -> Result[SummaryT, str]: ...


class TableDocumentService[DocumentT, PlanT, SummaryT]:
    """Encoding-neutral orchestration configured by one typed table policy."""

    def __init__(self, policy: TableDocumentPolicy[DocumentT, PlanT, SummaryT]) -> None:
        self._policy = policy

    @property
    def schema(self) -> Mapping[str, JsonValue]:
        """Return the generated schema configured by the table policy."""
        return self._policy.schema

    async def dump(self) -> Result[DocumentT, str]:
        """Build one complete typed document through the table policy."""
        return await self._policy.dump()

    async def dump_encoded(self, document_format: DocumentFormat) -> Result[TableDocumentDumpOutput, str]:
        """Build and encode one complete table document."""
        dump_result = await self._policy.dump()
        if dump_result.is_err:
            return Err(dump_result.unwrap_err())
        document = dump_result.unwrap()
        json_result = self._policy.to_json_document(document)
        if json_result.is_err:
            return Err(json_result.unwrap_err())
        encoded_result = encode_document(json_result.unwrap(), document_format)
        if encoded_result.is_err:
            return Err(encoded_result.unwrap_err())
        return Ok(
            TableDocumentDumpOutput(
                content=encoded_result.unwrap(),
                row_count=self._policy.row_count(document),
            )
        )

    async def plan_encoded(self, content: bytes, document_format: DocumentFormat) -> Result[PlanT, str]:
        """Decode, schema-validate, type, and plan one external document."""
        decoded_result = decode_document(content, document_format, self._policy.schema)
        if decoded_result.is_err:
            return Err(decoded_result.unwrap_err())
        typed_result = self._policy.from_json_document(decoded_result.unwrap())
        if typed_result.is_err:
            return Err(typed_result.unwrap_err())
        return await self._policy.plan(typed_result.unwrap())

    async def plan(self, document: DocumentT) -> Result[PlanT, str]:
        """Plan one already-typed document through the table policy."""
        return await self._policy.plan(document)

    async def apply(self, plan: PlanT) -> Result[SummaryT, str]:
        """Apply one validated plan through the table policy's store."""
        return await self._policy.apply(plan)
