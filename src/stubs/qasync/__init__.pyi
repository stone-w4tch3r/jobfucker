# # Type stub for `qasync` (a Qt + asyncio glue library).

# # qasync ships no type stubs and no inline annotations, so basedpyright otherwise
# # treats it as untyped (`reportMissingTypeStubs`) and every event-loop member
# # surfaces as `Unknown`. This vendored stub is the single owned typing for the
# # qasync surface the project uses (only `qasync.QEventLoop`). Because a real
# # `qasync.QEventLoop` is an `asyncio.BaseEventLoop` subclass, declaring it as one
# # here makes the loop fully ``asyncio``-typed: ``run_until_complete``,
# # ``run_forever``, ``call_soon``, ``close``, and context-manager use all resolve
# # via ``asyncio``. (The GUI that used it was removed; the rewrite per
# # `docs/ui/jobfucker.ui.md` will consume it again.)

# import asyncio

# __all__ = ["QEventLoop"]

# class QEventLoop(asyncio.BaseEventLoop):
#     """Bridge between a Qt event loop and ``asyncio``.

#     ``app`` is a ``QCoreApplication``/``QApplication`` (or ``None`` to discover
#     the running instance). The rest of qasync's surface is not used by the
#     project and is intentionally not declared here.
#     """

#     def __init__(self, app: object | None = None, **kwargs: object) -> None: ...

#     # Runtime asyncio loops are context managers, but typeshed's event-loop
#     # stubs omit the protocol; declare it here so ``with loop:`` type-checks.
#     def __enter__(self) -> QEventLoop: ...
#     def __exit__(
#         self,
#         exc_type: type[BaseException] | None,
#         exc_value: BaseException | None,
#         traceback: object,
#     ) -> None: ...
