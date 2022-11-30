import functools

from dbt.events.functions import warn_or_error
from dbt.events.types import JinjaLogWarning
from dbt.exceptions import (
    RuntimeException,
    CompilationException,
    missing_relation,
    raise_ambiguous_alias,
    raise_ambiguous_catalog_match,
    raise_cache_inconsistent,
    raise_dataclass_not_dict,
    raise_compiler_error,
    raise_database_error,
    raise_dep_not_found,
    raise_dependency_error,
    raise_duplicate_patch_name,
    raise_duplicate_resource_name,
    raise_invalid_property_yml_version,
    raise_not_implemented,
    relation_wrong_type,
)


def warn(msg, node=None):
    warn_or_error(JinjaLogWarning(msg=msg), node=node)
    return ""


class MissingConfigException(CompilationException):
    def __init__(self, unique_id, name):
        self.unique_id = unique_id
        self.name = name
        msg = (
            f"Model '{self.unique_id}' does not define a required config parameter '{self.name}'."
        )
        super().__init__(msg)


def missing_config(model, name):
    raise MissingConfigException(unique_id=model.unique_id, name=name)


class MissingMaterializationException(CompilationException):
    def __init__(self, model, adapter_type):
        self.model = model
        self.adapter_type = adapter_type
        super().__init__(self.get_message())

    def get_message(self) -> str:
        materialization = self.model.get_materialization()

        valid_types = "'default'"

        if self.adapter_type != "default":
            valid_types = f"'default' and '{self.adapter_type}'"

        msg = f"No materialization '{materialization}' was found for adapter {self.adapter_type}! (searched types {valid_types})"
        return msg


def missing_materialization(model, adapter_type):
    raise MissingConfigException(model=model, adapter_type=adapter_type)


# Update this when a new function should be added to the
# dbt context's `exceptions` key!
CONTEXT_EXPORTS = {
    fn.__name__: fn
    for fn in [
        warn,
        missing_config,
        missing_materialization,
        missing_relation,
        raise_ambiguous_alias,
        raise_ambiguous_catalog_match,
        raise_cache_inconsistent,
        raise_dataclass_not_dict,
        raise_compiler_error,
        raise_database_error,
        raise_dep_not_found,
        raise_dependency_error,
        raise_duplicate_patch_name,
        raise_duplicate_resource_name,
        raise_invalid_property_yml_version,
        raise_not_implemented,
        relation_wrong_type,
    ]
}


# wraps context based exceptions in node info
def wrapper(model):
    def wrap(func):
        @functools.wraps(func)
        def inner(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except RuntimeException as exc:
                exc.add_node(model)
                raise exc

        return inner

    return wrap


def wrapped_exports(model):
    wrap = wrapper(model)
    return {name: wrap(export) for name, export in CONTEXT_EXPORTS.items()}
