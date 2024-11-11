from __future__ import annotations

import warnings
from collections.abc import Callable, Generator, Iterable, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cached_property, partial
from importlib import import_module
from inspect import isclass
from itertools import chain
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    Protocol,
    TypedDict,
    TypeVar,
    cast,
    runtime_checkable,
)

if TYPE_CHECKING:
    from pydantic_settings import BaseSettings

L = TypeVar("L")


class DeprecatedImport(TypedDict):
    path: str
    reason: str
    new_attribute: str


def load(path: str, allow_splits: str = ":.") -> Any:
    splitted = path.rsplit(":", 1) if ":" in allow_splits else []
    if len(splitted) < 2 and "." in allow_splits:
        splitted = path.rsplit(".", 1)
    if len(splitted) != 2:
        raise ValueError(f"invalid path: {path}")
    module = import_module(splitted[0])
    return getattr(module, splitted[1])


def load_any(
    path: str, attrs: Sequence[str], *, non_first_deprecated: bool = False
) -> Any | None:
    module = import_module(path)
    first_name: None | str = None

    for attr in attrs:
        if hasattr(module, attr):
            if non_first_deprecated and first_name is not None:
                warnings.warn(
                    f'"{attr}" is deprecated, use "{first_name}" instead.',
                    DeprecationWarning,
                    stacklevel=2,
                )
            return getattr(module, attr)
        if first_name is None:
            first_name = attr
    raise ImportError(f"Could not import any of the attributes:.{', '.join(attrs)}")


@runtime_checkable
class ExtensionProtocol(Protocol[L]):
    name: str

    def apply(self, monkay_instance: Monkay[L]) -> None: ...


def _stub_previous_getattr(name: str) -> Any:
    raise AttributeError(f'Module has no attribute: "{name}" (Monkay).')


class Monkay(Generic[L]):
    _instance: None | L = None
    _instance_var: ContextVar[L | None] | None = None
    # extensions are pretended to always exist, we check the _extensions_var
    _extensions: dict[str, ExtensionProtocol[L]]
    _extensions_var: None | ContextVar[None | dict[str, ExtensionProtocol[L]]] = None
    _extensions_applied: None | ContextVar[dict[str, ExtensionProtocol[L]] | None] = (
        None
    )
    _settings_var: ContextVar[BaseSettings | None] | None = None

    def __init__(
        self,
        global_dict: dict,
        *,
        with_instance: str | bool = False,
        with_extensions: str | bool = False,
        extension_order_key_fn: None | Callable[[ExtensionProtocol[L]], Any] = None,
        settings_path: str = "",
        preloads: Iterable[str] = (),
        settings_preload_name: str = "",
        settings_extensions_name: str = "",
        lazy_imports: dict[str, str] | None = None,
        deprecated_lazy_imports: dict[str, DeprecatedImport] | None = None,
        settings_ctx_name: str = "monkay_settings_ctx",
        extensions_applied_ctx_name: str = "monkay_extensions_applied_ctx",
        skip_all_update: bool = False,
    ) -> None:
        if with_instance is True:
            with_instance = "monkay_instance_ctx"
        with_instance = with_instance
        if with_extensions is True:
            with_extensions = "monkay_extensions_ctx"
        with_extensions = with_extensions

        self._cache_imports: dict[str, Any] = {}
        self.lazy_imports = lazy_imports or {}
        self.deprecated_lazy_imports = deprecated_lazy_imports or {}
        assert set(
            self.lazy_imports
        ).isdisjoint(
            self.deprecated_lazy_imports
        ), f"Lazy imports and lazy deprecated imports share: {', '.join(set(self.lazy_imports).intersection(self.deprecated_lazy_imports))}"
        self.settings_path = settings_path
        if self.settings_path:
            self._settings_var = global_dict[settings_ctx_name] = ContextVar(
                settings_ctx_name, default=None
            )

        self.settings_preload_name = settings_preload_name
        self.settings_extensions_name = settings_extensions_name

        self._handle_preloads(preloads)
        if self.lazy_imports or self.deprecated_lazy_imports:
            getter: Callable[..., Any] = self.module_getter
            if "__getattr__" in global_dict:
                getter = partial(getter, chained_getter=global_dict["__getattr__"])
            global_dict["__getattr__"] = getter
            if not skip_all_update:
                all_var = global_dict.setdefault("__all__", [])
                global_dict["__all__"] = self.update_all_var(all_var)
        if with_instance:
            self._instance_var = global_dict[with_instance] = ContextVar(
                with_instance, default=None
            )
        if with_extensions:
            self.extension_order_key_fn = extension_order_key_fn
            self._extensions = {}
            self._extensions_var = global_dict[with_extensions] = ContextVar(
                with_extensions, default=None
            )
            self._extensions_applied_var = global_dict[extensions_applied_ctx_name] = (
                ContextVar(extensions_applied_ctx_name, default=None)
            )
            self._handle_extensions()

    @property
    def instance(self) -> L | None:
        assert self._instance_var is not None, "Monkay not enabled for instances"
        instance: L | None = self._instance_var.get()
        if instance is None:
            instance = self._instance
        return instance

    def set_instance(
        self,
        instance: L,
        apply_extensions: bool = True,
        use_extension_overwrite: bool = True,
    ) -> None:
        assert self._instance_var is not None, "Monkay not enabled for instances"
        # need to address before the instance is swapped
        if apply_extensions and self._extensions_applied_var.get() is not None:
            raise RuntimeError("Other apply process in the same context is active.")
        self._instance = instance
        if apply_extensions and self._extensions_var is not None:
            self.apply_extensions(use_overwrite=use_extension_overwrite)

    @contextmanager
    def with_instance(
        self,
        instance: L | None,
        apply_extensions: bool = False,
        use_extension_overwrite: bool = True,
    ) -> Generator:
        assert self._instance_var is not None, "Monkay not enabled for instances"
        # need to address before the instance is swapped
        if apply_extensions and self._extensions_applied_var.get() is not None:
            raise RuntimeError("Other apply process in the same context is active.")
        token = self._instance_var.set(instance)
        try:
            if apply_extensions and self._extensions_var is not None:
                self.apply_extensions(use_overwrite=use_extension_overwrite)
            yield
        finally:
            self._instance_var.reset(token)

    def apply_extensions(self, use_overwrite: bool = True) -> None:
        assert self._extensions_var is not None, "Monkay not enabled for extensions"
        extensions: dict[str, ExtensionProtocol[L]] | None = (
            self._extensions_var.get() if use_overwrite else None
        )
        if extensions is None:
            extensions = self._extensions
        extensions_applied = self._extensions_applied_var.get()
        if extensions_applied is not None:
            raise RuntimeError("Other apply process in the same context is active.")
        extensions_ordered: Iterable[tuple[str, ExtensionProtocol[L]]] = cast(
            dict[str, ExtensionProtocol[L]], extensions
        ).items()

        if self.extension_order_key_fn is not None:
            extensions_ordered = sorted(
                extensions_ordered,
                key=self.extension_order_key_fn,  # type:  ignore
            )
        extensions_applied = set()
        token = self._extensions_applied_var.set(extensions_applied)
        try:
            for name, extension in extensions_ordered:
                if name in extensions_applied:
                    continue
                extensions_applied.add(name)
                extension.apply(self)
        finally:
            self._extensions_applied_var.reset(token)

    def ensure_extension(self, name_or_extension: str | ExtensionProtocol[L]) -> None:
        assert self._extensions_var is not None, "Monkay not enabled for extensions"
        extensions: dict[str, ExtensionProtocol[L]] | None = self._extensions_var.get()
        if extensions is None:
            extensions = self._extensions
        if isinstance(name_or_extension, str):
            name = name_or_extension
            extension = extensions.get(name)
        elif not isclass(name_or_extension) and isinstance(
            name_or_extension, ExtensionProtocol
        ):
            name = name_or_extension.name
            extension = extensions.get(name, name_or_extension)
        else:
            raise RuntimeError(
                'Provided extension "{name_or_extension}" does not implement the ExtensionProtocol'
            )
        if name in self._extensions_applied_var.get():
            return

        if extension is None:
            raise RuntimeError(f'Extension: "{name}" does not exist.')
        self._extensions_applied_var.get().add(name)
        extension.apply(self)

    def add_extension(
        self,
        extension: ExtensionProtocol[L]
        | type[ExtensionProtocol[L]]
        | Callable[[], ExtensionProtocol[L]],
        use_overwrite: bool = True,
    ) -> None:
        assert self._extensions_var is not None, "Monkay not enabled for extensions"
        extensions: dict[str, ExtensionProtocol[L]] | None = (
            self._extensions_var.get() if use_overwrite else None
        )
        if extensions is None:
            extensions = self._extensions
        if callable(extension) or isclass(extension):
            extension = extension()
        if not isinstance(extension, ExtensionProtocol):
            raise ValueError(f"Extension {extension} is not compatible")
        extensions[extension.name] = extension

    @contextmanager
    def with_extensions(
        self,
        extensions: dict[str, ExtensionProtocol[L]] | None,
        apply_extensions: bool = False,
    ) -> Generator:
        assert self._extensions_var is not None, "Monkay not enabled for extensions"
        token = self._extensions_var.set(extensions)
        try:
            yield
        finally:
            self._extensions_var.reset(token)

    def update_all_var(self, all_var: Sequence[str]) -> list[str]:
        if not isinstance(all_var, list):
            all_var = list(all_var)
        all_var_set = set(all_var)
        if self.lazy_imports or self.deprecated_lazy_imports:
            for var in chain(
                self.lazy_imports,
                self.deprecated_lazy_imports,
            ):
                if var not in all_var_set:
                    all_var.append(var)
        return all_var

    @cached_property
    def _settings(self) -> BaseSettings:
        settings: Any = load(self.settings_path)
        if isclass(settings):
            settings = settings()
        return settings

    @property
    def settings(self) -> BaseSettings:
        assert self._settings_var is not None, "Monkay not enabled for settings"
        settings = self._settings_var.get()
        if settings is None:
            settings = self._settings
        return settings

    @contextmanager
    def with_settings(self, settings: BaseSettings | None) -> Generator:
        assert self._settings_var is not None, "Monkay not enabled for settings"
        token = self._settings_var.set(settings)
        try:
            yield
        finally:
            self._settings_var.reset(token)

    def module_getter(
        self, key: str, *, chained_getter: Callable[[str], Any] = _stub_previous_getattr
    ) -> Any:
        lazy_import = self.lazy_imports.get(key)
        if lazy_import is None:
            deprecated = self.deprecated_lazy_imports.get(key)
            if deprecated is not None:
                lazy_import = deprecated["path"]
                warn_strs = [f'Attribute: "{key}" is deprecated.']
                if deprecated.get("reason"):
                    warn_strs.append(f"Reason: {deprecated['reason']}.")
                if deprecated.get("new_attribute"):
                    warn_strs.append(f'Use "{deprecated["new_attribute"]}" instead.')
                warnings.warn("\n".join(warn_strs), DeprecationWarning, stacklevel=2)

        if lazy_import is None:
            return chained_getter(key)
        if key not in self._cache_imports:
            self._cache_imports[key] = load(lazy_import)
        return self._cache_imports[key]

    def _handle_preloads(self, preloads: Iterable[str]) -> None:
        if self.settings_preload_name:
            preloads = chain(
                preloads, getattr(self.settings, self.settings_preload_name)
            )
        for preload in preloads:
            splitted = preload.rsplit(":", 1)
            try:
                module = import_module(splitted[0])
            except ImportError:
                module = None
            if module is not None and len(splitted) == 2:
                getattr(module, splitted[1])()

    def _handle_extensions(self) -> None:
        if self.settings_extensions_name:
            for extension in getattr(self.settings, self.settings_extensions_name):
                self.add_extension(extension, use_overwrite=False)
