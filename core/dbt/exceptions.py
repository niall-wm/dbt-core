import builtins
from typing import NoReturn, Optional, Mapping, Any

from dbt.events.helpers import env_secrets, scrub_secrets
from dbt.events.types import JinjaLogWarning
from dbt.events.contextvars import get_node_info
from dbt.node_types import NodeType
from dbt.jinja_exceptions import (
    warn,
    missing_config,
    missing_materialization,
    missing_relation,
    raise_ambiguous_alias,
    raise_ambiguous_catalog_match,
    raise_cache_inconsistent,
    raise_dataclass_not_dict,
    # raise_compiler_error,
    raise_database_error,
    raise_dep_not_found,
    raise_dependency_error,
    raise_duplicate_patch_name,
    raise_duplicate_resource_name,
    raise_invalid_property_yml_version,
    raise_not_implemented,
    relation_wrong_type,
)

import dbt.dataclass_schema


def validator_error_message(exc):
    """Given a dbt.dataclass_schema.ValidationError (which is basically a
    jsonschema.ValidationError), return the relevant parts as a string
    """
    if not isinstance(exc, dbt.dataclass_schema.ValidationError):
        return str(exc)
    path = "[%s]" % "][".join(map(repr, exc.relative_path))
    return "at path {}: {}".format(path, exc.message)


class Exception(builtins.Exception):
    CODE = -32000
    MESSAGE = "Server Error"

    def data(self):
        # if overriding, make sure the result is json-serializable.
        return {
            "type": self.__class__.__name__,
            "message": str(self),
        }


class MacroReturn(builtins.BaseException):
    """
    Hack of all hacks
    """

    def __init__(self, value):
        self.value = value


class InternalException(Exception):
    pass


class RuntimeException(RuntimeError, Exception):
    CODE = 10001
    MESSAGE = "Runtime error"

    def __init__(self, msg, node=None):
        self.stack = []
        self.node = node
        self.msg = scrub_secrets(msg, env_secrets())

    def add_node(self, node=None):
        if node is not None and node is not self.node:
            if self.node is not None:
                self.stack.append(self.node)
            self.node = node

    @property
    def type(self):
        return "Runtime"

    def node_to_string(self, node):
        if node is None:
            return "<Unknown>"
        if not hasattr(node, "name"):
            # we probably failed to parse a block, so we can't know the name
            return "{} ({})".format(node.resource_type, node.original_file_path)

        if hasattr(node, "contents"):
            # handle FileBlocks. They aren't really nodes but we want to render
            # out the path we know at least. This indicates an error during
            # block parsing.
            return "{}".format(node.path.original_file_path)
        return "{} {} ({})".format(node.resource_type, node.name, node.original_file_path)

    def process_stack(self):
        lines = []
        stack = self.stack + [self.node]
        first = True

        if len(stack) > 1:
            lines.append("")

            for item in stack:
                msg = "called by"

                if first:
                    msg = "in"
                    first = False

                lines.append("> {} {}".format(msg, self.node_to_string(item)))

        return lines

    def __str__(self, prefix="! "):
        node_string = ""

        if self.node is not None:
            node_string = " in {}".format(self.node_to_string(self.node))

        if hasattr(self.msg, "split"):
            split_msg = self.msg.split("\n")
        else:
            split_msg = str(self.msg).split("\n")

        lines = ["{}{}".format(self.type + " Error", node_string)] + split_msg

        lines += self.process_stack()

        return lines[0] + "\n" + "\n".join(["  " + line for line in lines[1:]])

    def data(self):
        result = Exception.data(self)
        if self.node is None:
            return result

        result.update(
            {
                "raw_code": self.node.raw_code,
                # the node isn't always compiled, but if it is, include that!
                "compiled_code": getattr(self.node, "compiled_code", None),
            }
        )
        return result


class RPCFailureResult(RuntimeException):
    CODE = 10002
    MESSAGE = "RPC execution error"


class RPCTimeoutException(RuntimeException):
    CODE = 10008
    MESSAGE = "RPC timeout error"

    def __init__(self, timeout):
        super().__init__(self.MESSAGE)
        self.timeout = timeout

    def data(self):
        result = super().data()
        result.update(
            {
                "timeout": self.timeout,
                "message": "RPC timed out after {}s".format(self.timeout),
            }
        )
        return result


class RPCKilledException(RuntimeException):
    CODE = 10009
    MESSAGE = "RPC process killed"

    def __init__(self, signum):
        self.signum = signum
        self.message = "RPC process killed by signal {}".format(self.signum)
        super().__init__(self.message)

    def data(self):
        return {
            "signum": self.signum,
            "message": self.message,
        }


class RPCCompiling(RuntimeException):
    CODE = 10010
    MESSAGE = 'RPC server is compiling the project, call the "status" method for' " compile status"

    def __init__(self, msg=None, node=None):
        if msg is None:
            msg = "compile in progress"
        super().__init__(msg, node)


class RPCLoadException(RuntimeException):
    CODE = 10011
    MESSAGE = (
        'RPC server failed to compile project, call the "status" method for' " compile status"
    )

    def __init__(self, cause):
        self.cause = cause
        self.message = "{}: {}".format(self.MESSAGE, self.cause["message"])
        super().__init__(self.message)

    def data(self):
        return {"cause": self.cause, "message": self.message}


class DatabaseException(RuntimeException):
    CODE = 10003
    MESSAGE = "Database Error"

    def process_stack(self):
        lines = []

        if hasattr(self.node, "build_path") and self.node.build_path:
            lines.append("compiled Code at {}".format(self.node.build_path))

        return lines + RuntimeException.process_stack(self)

    @property
    def type(self):
        return "Database"


class CompilationException(RuntimeException):
    CODE = 10004
    MESSAGE = "Compilation Error"

    @property
    def type(self):
        return "Compilation"


class RecursionException(RuntimeException):
    pass


class ValidationException(RuntimeException):
    CODE = 10005
    MESSAGE = "Validation Error"


class ParsingException(RuntimeException):
    CODE = 10015
    MESSAGE = "Parsing Error"

    @property
    def type(self):
        return "Parsing"


class JSONValidationException(ValidationException):
    def __init__(self, typename, errors):
        self.typename = typename
        self.errors = errors
        self.errors_message = ", ".join(errors)
        msg = 'Invalid arguments passed to "{}" instance: {}'.format(
            self.typename, self.errors_message
        )
        super().__init__(msg)

    def __reduce__(self):
        # see https://stackoverflow.com/a/36342588 for why this is necessary
        return (JSONValidationException, (self.typename, self.errors))


class IncompatibleSchemaException(RuntimeException):
    def __init__(self, expected: str, found: Optional[str]):
        self.expected = expected
        self.found = found
        self.filename = "input file"

        super().__init__(self.get_message())

    def add_filename(self, filename: str):
        self.filename = filename
        self.msg = self.get_message()

    def get_message(self) -> str:
        found_str = "nothing"
        if self.found is not None:
            found_str = f'"{self.found}"'

        msg = (
            f'Expected a schema version of "{self.expected}" in '
            f"{self.filename}, but found {found_str}. Are you running with a "
            f"different version of dbt?"
        )
        return msg

    CODE = 10014
    MESSAGE = "Incompatible Schema"


class JinjaRenderingException(CompilationException):
    pass


class UndefinedMacroException(CompilationException):
    def __str__(self, prefix="! ") -> str:
        msg = super().__str__(prefix)
        return (
            f"{msg}. This can happen when calling a macro that does "
            "not exist. Check for typos and/or install package dependencies "
            'with "dbt deps".'
        )


class UnknownAsyncIDException(Exception):
    CODE = 10012
    MESSAGE = "RPC server got an unknown async ID"

    def __init__(self, task_id):
        self.task_id = task_id

    def __str__(self):
        return "{}: {}".format(self.MESSAGE, self.task_id)


class AliasException(ValidationException):
    pass


class DependencyException(Exception):
    # this can happen due to raise_dependency_error and its callers
    CODE = 10006
    MESSAGE = "Dependency Error"


class DbtConfigError(RuntimeException):
    CODE = 10007
    MESSAGE = "DBT Configuration Error"

    def __init__(self, message, project=None, result_type="invalid_project", path=None):
        self.project = project
        super().__init__(message)
        self.result_type = result_type
        self.path = path

    def __str__(self, prefix="! ") -> str:
        msg = super().__str__(prefix)
        if self.path is None:
            return msg
        else:
            return f"{msg}\n\nError encountered in {self.path}"


class FailFastException(RuntimeException):
    CODE = 10013
    MESSAGE = "FailFast Error"

    def __init__(self, message, result=None, node=None):
        super().__init__(msg=message, node=node)
        self.result = result

    @property
    def type(self):
        return "FailFast"


class DbtProjectError(DbtConfigError):
    pass


class DbtSelectorsError(DbtConfigError):
    pass


class DbtProfileError(DbtConfigError):
    pass


class SemverException(Exception):
    def __init__(self, msg=None):
        self.msg = msg
        if msg is not None:
            super().__init__(msg)
        else:
            super().__init__()


class VersionsNotCompatibleException(SemverException):
    pass


class NotImplementedException(Exception):
    pass


class FailedToConnectException(DatabaseException):
    pass


class CommandError(RuntimeException):
    def __init__(self, cwd, cmd, message="Error running command"):
        cmd_scrubbed = list(scrub_secrets(cmd_txt, env_secrets()) for cmd_txt in cmd)
        super().__init__(message)
        self.cwd = cwd
        self.cmd = cmd_scrubbed
        self.args = (cwd, cmd_scrubbed, message)

    def __str__(self):
        if len(self.cmd) == 0:
            return "{}: No arguments given".format(self.msg)
        return '{}: "{}"'.format(self.msg, self.cmd[0])


class ExecutableError(CommandError):
    def __init__(self, cwd, cmd, message):
        super().__init__(cwd, cmd, message)


class WorkingDirectoryError(CommandError):
    def __init__(self, cwd, cmd, message):
        super().__init__(cwd, cmd, message)

    def __str__(self):
        return '{}: "{}"'.format(self.msg, self.cwd)


class CommandResultError(CommandError):
    def __init__(self, cwd, cmd, returncode, stdout, stderr, message="Got a non-zero returncode"):
        super().__init__(cwd, cmd, message)
        self.returncode = returncode
        self.stdout = scrub_secrets(stdout.decode("utf-8"), env_secrets())
        self.stderr = scrub_secrets(stderr.decode("utf-8"), env_secrets())
        self.args = (cwd, self.cmd, returncode, self.stdout, self.stderr, message)

    def __str__(self):
        return "{} running: {}".format(self.msg, self.cmd)


class InvalidConnectionException(RuntimeException):
    def __init__(self, thread_id, known, node=None):
        self.thread_id = thread_id
        self.known = known
        super().__init__(
            msg="connection never acquired for thread {}, have {}".format(
                self.thread_id, self.known
            )
        )


class InvalidSelectorException(RuntimeException):
    def __init__(self, name: str):
        self.name = name
        super().__init__(name)


class DuplicateYamlKeyException(CompilationException):
    pass


# TODO: this was copied into jinja_exxceptions because it's in the context - eventually remove?
def raise_compiler_error(msg, node=None) -> NoReturn:
    raise CompilationException(msg, node)


def raise_parsing_error(msg, node=None) -> NoReturn:
    raise ParsingException(msg, node)


def raise_git_cloning_error(error: CommandResultError) -> NoReturn:
    error.cmd = scrub_secrets(str(error.cmd), env_secrets())
    raise error


def raise_git_cloning_problem(repo) -> NoReturn:
    repo = scrub_secrets(repo, env_secrets())
    msg = """\
    Something went wrong while cloning {}
    Check the debug logs for more information
    """
    raise RuntimeException(msg.format(repo))


def disallow_secret_env_var(env_var_name) -> NoReturn:
    """Raise an error when a secret env var is referenced outside allowed
    rendering contexts"""
    msg = (
        "Secret env vars are allowed only in profiles.yml or packages.yml. "
        "Found '{env_var_name}' referenced elsewhere."
    )
    raise_parsing_error(msg.format(env_var_name=env_var_name))


def invalid_type_error(
    method_name, arg_name, got_value, expected_type, version="0.13.0"
) -> NoReturn:
    """Raise a CompilationException when an adapter method available to macros
    has changed.
    """
    got_type = type(got_value)
    msg = (
        "As of {version}, 'adapter.{method_name}' expects argument "
        "'{arg_name}' to be of type '{expected_type}', instead got "
        "{got_value} ({got_type})"
    )
    raise_compiler_error(
        msg.format(
            version=version,
            method_name=method_name,
            arg_name=arg_name,
            expected_type=expected_type,
            got_value=got_value,
            got_type=got_type,
        )
    )


def invalid_bool_error(got_value, macro_name) -> NoReturn:
    """Raise a CompilationException when a macro expects a boolean but gets some
    other value.
    """
    msg = (
        "Macro '{macro_name}' returns '{got_value}'.  It is not type 'bool' "
        "and cannot not be converted reliably to a bool."
    )
    raise_compiler_error(msg.format(macro_name=macro_name, got_value=got_value))


def ref_invalid_args(model, args) -> NoReturn:
    raise_compiler_error("ref() takes at most two arguments ({} given)".format(len(args)), model)


def metric_invalid_args(model, args) -> NoReturn:
    raise_compiler_error(
        "metric() takes at most two arguments ({} given)".format(len(args)), model
    )


def ref_bad_context(model, args) -> NoReturn:
    ref_args = ", ".join("'{}'".format(a) for a in args)
    ref_string = "{{{{ ref({}) }}}}".format(ref_args)

    base_error_msg = """dbt was unable to infer all dependencies for the model "{model_name}".
This typically happens when ref() is placed within a conditional block.

To fix this, add the following hint to the top of the model "{model_name}":

-- depends_on: {ref_string}"""
    # This explicitly references model['name'], instead of model['alias'], for
    # better error messages. Ex. If models foo_users and bar_users are aliased
    # to 'users', in their respective schemas, then you would want to see
    # 'bar_users' in your error messge instead of just 'users'.
    if isinstance(model, dict):  # TODO: remove this path
        model_name = model["name"]
        model_path = model["path"]
    else:
        model_name = model.name
        model_path = model.path
    error_msg = base_error_msg.format(
        model_name=model_name, model_path=model_path, ref_string=ref_string
    )
    raise_compiler_error(error_msg, model)


def doc_invalid_args(model, args) -> NoReturn:
    raise_compiler_error("doc() takes at most two arguments ({} given)".format(len(args)), model)


def doc_target_not_found(
    model, target_doc_name: str, target_doc_package: Optional[str]
) -> NoReturn:
    target_package_string = ""

    if target_doc_package is not None:
        target_package_string = "in package '{}' ".format(target_doc_package)

    msg = ("Documentation for '{}' depends on doc '{}' {} which was not found").format(
        model.unique_id, target_doc_name, target_package_string
    )
    raise_compiler_error(msg, model)


def get_not_found_or_disabled_msg(
    original_file_path,
    unique_id,
    resource_type_title,
    target_name: str,
    target_kind: str,
    target_package: Optional[str] = None,
    disabled: Optional[bool] = None,
) -> str:
    if disabled is None:
        reason = "was not found or is disabled"
    elif disabled is True:
        reason = "is disabled"
    else:
        reason = "was not found"

    target_package_string = ""
    if target_package is not None:
        target_package_string = "in package '{}' ".format(target_package)

    return "{} '{}' ({}) depends on a {} named '{}' {}which {}".format(
        resource_type_title,
        unique_id,
        original_file_path,
        target_kind,
        target_name,
        target_package_string,
        reason,
    )


def target_not_found(
    node,
    target_name: str,
    target_kind: str,
    target_package: Optional[str] = None,
    disabled: Optional[bool] = None,
) -> NoReturn:
    msg = get_not_found_or_disabled_msg(
        original_file_path=node.original_file_path,
        unique_id=node.unique_id,
        resource_type_title=node.resource_type.title(),
        target_name=target_name,
        target_kind=target_kind,
        target_package=target_package,
        disabled=disabled,
    )

    raise_compiler_error(msg, node)


def dependency_not_found(model, target_model_name):
    raise_compiler_error(
        "'{}' depends on '{}' which is not in the graph!".format(
            model.unique_id, target_model_name
        ),
        model,
    )


def macro_not_found(model, target_macro_id):
    raise_compiler_error(
        model,
        "'{}' references macro '{}' which is not defined!".format(
            model.unique_id, target_macro_id
        ),
    )


def macro_invalid_dispatch_arg(macro_name) -> NoReturn:
    msg = """\
    The "packages" argument of adapter.dispatch() has been deprecated.
    Use the "macro_namespace" argument instead.

    Raised during dispatch for: {}

    For more information, see:

    https://docs.getdbt.com/reference/dbt-jinja-functions/dispatch
    """
    raise_compiler_error(msg.format(macro_name))


def materialization_not_available(model, adapter_type):
    materialization = model.get_materialization()

    raise_compiler_error(
        "Materialization '{}' is not available for {}!".format(materialization, adapter_type),
        model,
    )


def bad_package_spec(repo, spec, error_message):
    msg = "Error checking out spec='{}' for repo {}\n{}".format(spec, repo, error_message)
    raise InternalException(scrub_secrets(msg, env_secrets()))


def invalid_materialization_argument(name, argument):
    raise_compiler_error(
        "materialization '{}' received unknown argument '{}'.".format(name, argument)
    )


def system_error(operation_name):
    raise_compiler_error(
        "dbt encountered an error when attempting to {}. "
        "If this error persists, please create an issue at: \n\n"
        "https://github.com/dbt-labs/dbt-core".format(operation_name)
    )


class ConnectionException(Exception):
    """
    There was a problem with the connection that returned a bad response,
    timed out, or resulted in a file that is corrupt.
    """

    pass


def multiple_matching_relations(kwargs, matches):
    raise_compiler_error(
        "get_relation returned more than one relation with the given args. "
        "Please specify a database or schema to narrow down the result set."
        "\n{}\n\n{}".format(kwargs, matches)
    )


def get_relation_returned_multiple_results(kwargs, matches):
    multiple_matching_relations(kwargs, matches)


def approximate_relation_match(target, relation):
    raise_compiler_error(
        "When searching for a relation, dbt found an approximate match. "
        "Instead of guessing \nwhich relation to use, dbt will move on. "
        "Please delete {relation}, or rename it to be less ambiguous."
        "\nSearched for: {target}\nFound: {relation}".format(target=target, relation=relation)
    )


def raise_duplicate_macro_name(node_1, node_2, namespace) -> NoReturn:
    duped_name = node_1.name
    if node_1.package_name != node_2.package_name:
        extra = ' ("{}" and "{}" are both in the "{}" namespace)'.format(
            node_1.package_name, node_2.package_name, namespace
        )
    else:
        extra = ""

    raise_compiler_error(
        'dbt found two macros with the name "{}" in the namespace "{}"{}. '
        "Since these macros have the same name and exist in the same "
        "namespace, dbt will be unable to decide which to call. To fix this, "
        "change the name of one of these macros:\n- {} ({})\n- {} ({})".format(
            duped_name,
            namespace,
            extra,
            node_1.unique_id,
            node_1.original_file_path,
            node_2.unique_id,
            node_2.original_file_path,
        )
    )


def raise_patch_targets_not_found(patches):
    patch_list = "\n\t".join(
        "model {} (referenced in path {})".format(p.name, p.original_file_path)
        for p in patches.values()
    )
    raise_compiler_error(
        "dbt could not find models for the following patches:\n\t{}".format(patch_list)
    )


def _fix_dupe_msg(path_1: str, path_2: str, name: str, type_name: str) -> str:
    if path_1 == path_2:
        return f"remove one of the {type_name} entries for {name} in this file:\n - {path_1!s}\n"
    else:
        return (
            f"remove the {type_name} entry for {name} in one of these files:\n"
            f" - {path_1!s}\n{path_2!s}"
        )


def raise_duplicate_macro_patch_name(patch_1, existing_patch_path):
    package_name = patch_1.package_name
    name = patch_1.name
    fix = _fix_dupe_msg(patch_1.original_file_path, existing_patch_path, name, "macros")
    raise_compiler_error(
        f"dbt found two schema.yml entries for the same macro in package "
        f"{package_name} named {name}. Macros may only be described a single "
        f"time. To fix this, {fix}"
    )


def raise_duplicate_source_patch_name(patch_1, patch_2):
    name = f"{patch_1.overrides}.{patch_1.name}"
    fix = _fix_dupe_msg(
        patch_1.path,
        patch_2.path,
        name,
        "sources",
    )
    raise_compiler_error(
        f"dbt found two schema.yml entries for the same source named "
        f"{patch_1.name} in package {patch_1.overrides}. Sources may only be "
        f"overridden a single time. To fix this, {fix}"
    )


def raise_unrecognized_credentials_type(typename, supported_types):
    raise_compiler_error(
        'Unrecognized credentials type "{}" - supported types are ({})'.format(
            typename, ", ".join('"{}"'.format(t) for t in supported_types)
        )
    )


def raise_duplicate_alias(
    kwargs: Mapping[str, Any], aliases: Mapping[str, str], canonical_key: str
) -> NoReturn:
    # dupe found: go through the dict so we can have a nice-ish error
    key_names = ", ".join("{}".format(k) for k in kwargs if aliases.get(k) == canonical_key)

    raise AliasException(f'Got duplicate keys: ({key_names}) all map to "{canonical_key}"')


def warn(msg, node=None):
    dbt.events.functions.warn_or_error(
        JinjaLogWarning(msg=msg, node_info=get_node_info()),
        node=node,
    )
    return ""


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
# adapters exceptions
class UnexpectedNull(DatabaseException):
    def __init__(self, field_name, source):
        self.field_name = field_name
        self.source = source
        msg = (
            f"Expected a non-null value when querying field '{self.field_name}' of table "
            f" {self.source} but received value 'null' instead"
        )
        super().__init__(msg)


class UnexpectedNonTimestamp(DatabaseException):
    def __init__(self, field_name, source, dt):
        self.field_name = field_name
        self.source = source
        self.type_name = type(dt).__name__
        msg = (
            f"Expected a timestamp value when querying field '{self.field_name}' of table "
            f"{self.source} but received value of type '{self.type_name}' instead"
        )
        super().__init__(msg)


# start new exceptions


# deps exceptions
class MultipleVersionGitDeps(DependencyException):
    def __init__(self, git, requested):
        self.git = git
        self.requested = requested
        msg = (
            "git dependencies should contain exactly one version. "
            f"{self.git} contains: {self.requested}"
        )
        super().__init__(msg)


class DuplicateProjectDependency(DependencyException):
    def __init__(self, project_name):
        self.project_name = project_name
        msg = (
            f'Found duplicate project "{self.project_name}". This occurs when '
            "a dependency has the same project name as some other dependency."
        )
        super().__init__(msg)


class DuplicateDependencyToRoot(DependencyException):
    def __init__(self, project_name):
        self.project_name = project_name
        msg = (
            "Found a dependency with the same name as the root project "
            f'"{self.project_name}". Package names must be unique in a project.'
            " Please rename one of these packages."
        )
        super().__init__(msg)


class MismatchedDependencyTypes(DependencyException):
    def __init__(self, new, old):
        self.new = new
        self.old = old
        msg = (
            f"Cannot incorporate {self.new} ({self.new.__class__.__name__}) in {self.old} "
            f"({self.old.__class__.__name__}): mismatched types"
        )
        super().__init__(msg)


class PackageVersionNotFound(DependencyException):
    def __init__(self, package_name, version_range, available_versions, should_version_check):
        self.package_name = package_name
        self.version_range = version_range
        self.available_versions = available_versions
        self.should_version_check = should_version_check
        super().__init__(self.get_message())

    def get_message(self) -> str:
        base_msg = (
            "Could not find a matching compatible version for package {}\n"
            "  Requested range: {}\n"
            "  Compatible versions: {}\n"
        )
        addendum = (
            (
                "\n"
                "  Not shown: package versions incompatible with installed version of dbt-core\n"
                "  To include them, run 'dbt --no-version-check deps'"
            )
            if self.should_version_check
            else ""
        )
        msg = (
            base_msg.format(self.package_name, self.version_range, self.available_versions)
            + addendum
        )
        return msg


class PackageNotFound(DependencyException):
    def __init__(self, package_name):
        self.package_name = package_name
        msg = f"Package {self.package_name} was not found in the package index"
        super().__init__(msg)


# jinja exceptions
class MissingConfig(CompilationException):
    def __init__(self, unique_id, name):
        self.unique_id = unique_id
        self.name = name
        msg = (
            f"Model '{self.unique_id}' does not define a required config parameter '{self.name}'."
        )
        super().__init__(msg)


class MissingMaterialization(CompilationException):
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


class MissingRelation(CompilationException):
    def __init__(self, relation, model=None):
        self.relation = relation
        self.model = model
        msg = f"Relation {self.relation} not found!"
        super().__init__(msg)


class AmbiguousAlias(CompilationException):
    def __init__(self, node_1, node_2, duped_name=None):
        self.node_1 = node_1
        self.node_2 = node_2
        if duped_name is None:
            self.duped_name = f"{self.node_1.database}.{self.node_1.schema}.{self.node_1.alias}"
        else:
            self.duped_name = duped_name
        super().__init__(self.get_message())

    def get_message(self) -> str:

        msg = (
            'dbt found two resources with the database representation "{}".\ndbt '
            "cannot create two resources with identical database representations. "
            "To fix this,\nchange the configuration of one of these resources:"
            "\n- {} ({})\n- {} ({})".format(
                self.duped_name,
                self.node_1.unique_id,
                self.node_1.original_file_path,
                self.node_2.unique_id,
                self.node_2.original_file_path,
            )
        )
        return msg


class AmbiguousCatalogMatch(CompilationException):
    def __init__(self, unique_id, match_1, match_2):
        self.unique_id = unique_id
        self.match_1 = match_1
        self.match_2 = match_2
        super().__init__(self.get_message())

    def get_match_string(self, match):
        return "{}.{}".format(
            match.get("metadata", {}).get("schema"),
            match.get("metadata", {}).get("name"),
        )

    def get_message(self) -> str:
        msg = (
            "dbt found two relations in your warehouse with similar database identifiers. "
            "dbt\nis unable to determine which of these relations was created by the model "
            f'"{self.unique_id}".\nIn order for dbt to correctly generate the catalog, one '
            "of the following relations must be deleted or renamed:\n\n - "
            f"{self.get_match_string(self.match_1)}\n - {self.get_match_string(self.match_2)}"
        )

        return msg


class CacheInconsistency(InternalException):
    def __init__(self, message):
        self.message = message
        msg = f"Cache inconsistency detected: {self.message}"
        super().__init__(msg)


# this is part of the context and also raised in dbt.contratcs.relation.py
class DataclassNotDict(CompilationException):
    def __init__(self, obj):
        self.obj = obj  # TODO: what kind of obj is this?
        super().__init__(self.get_message())

    def get_message(self) -> str:
        msg = (
            f'The object ("{self.obj}") was used as a dictionary. This '
            "capability has been removed from objects of this type."
        )

        return msg


class DependencyNotFound(CompilationException):
    def __init__(self, node, node_description, required_pkg):
        self.node = node
        self.node_description = node_description
        self.required_pkg = required_pkg
        super().__init__(self.get_message())

    def get_message(self) -> str:
        msg = (
            f"Error while parsing {self.node_description}.\nThe required package "
            f'"{self.required_pkg}" was not found. Is the package installed?\n'
            "Hint: You may need to run `dbt deps`."
        )

        return msg


class DuplicatePatchPath(CompilationException):
    def __init__(self, patch_1, existing_patch_path):
        self.patch_1 = patch_1
        self.existing_patch_path = existing_patch_path
        super().__init__(self.get_message())

    def get_message(self) -> str:
        name = self.patch_1.name
        fix = _fix_dupe_msg(
            self.patch_1.original_file_path,
            self.existing_patch_path,
            name,
            "resource",
        )
        msg = (
            f"dbt found two schema.yml entries for the same resource named "
            f"{name}. Resources and their associated columns may only be "
            f"described a single time. To fix this, {fix}"
        )
        return msg


# should this inherit ParsingException instead?
class DuplicateResourceName(CompilationException):
    def __init__(self, node_1, node_2):
        self.node_1 = node_1
        self.node_2 = node_2
        super().__init__(self.get_message())

    def get_message(self) -> str:
        duped_name = self.node_1.name
        node_type = NodeType(self.node_1.resource_type)
        pluralized = (
            node_type.pluralize()
            if self.node_1.resource_type == self.node_2.resource_type
            else "resources"  # still raise if ref() collision, e.g. model + seed
        )

        action = "looking for"
        # duplicate 'ref' targets
        if node_type in NodeType.refable():
            formatted_name = f'ref("{duped_name}")'
        # duplicate sources
        elif node_type == NodeType.Source:
            duped_name = self.node_1.get_full_source_name()
            formatted_name = self.node_1.get_source_representation()
        # duplicate docs blocks
        elif node_type == NodeType.Documentation:
            formatted_name = f'doc("{duped_name}")'
        # duplicate generic tests
        elif node_type == NodeType.Test and hasattr(self.node_1, "test_metadata"):
            column_name = (
                f'column "{self.node_1.column_name}" in ' if self.node_1.column_name else ""
            )
            model_name = self.node_1.file_key_name
            duped_name = f'{self.node_1.name}" defined on {column_name}"{model_name}'
            action = "running"
            formatted_name = "tests"
        # all other resource types
        else:
            formatted_name = duped_name

        msg = f"""
dbt found two {pluralized} with the name "{duped_name}".

Since these resources have the same name, dbt will be unable to find the correct resource
when {action} {formatted_name}.

To fix this, change the name of one of these resources:
- {self.node_1.unique_id} ({self.node_1.original_file_path})
- {self.node_2.unique_id} ({self.node_2.original_file_path})
    """.strip()
        return msg


class InvalidPropertyYML(CompilationException):
    def __init__(self, path, issue):
        self.path = path
        self.issue = issue
        super().__init__(self.get_message())

    def get_message(self) -> str:
        msg = (
            f"The yml property file at {self.path} is invalid because {self.issue}. "
            "Please consult the documentation for more information on yml property file "
            "syntax:\n\nhttps://docs.getdbt.com/reference/configs-and-properties"
        )
        return msg


class PropertyYMLMissingVersion(InvalidPropertyYML):
    def __init__(self, path):
        self.path = path
        self.issue = f"the yml property file {self.path} is missing a version tag"
        super().__init__()


class PropertyYMLVersionNotInt(InvalidPropertyYML):
    def __init__(self, path, version):
        self.path = path
        self.version = version
        self.issue = (
            "its 'version:' tag must be an integer (e.g. version: 2)."
            f" {self.version} is not an integer"
        )
        super().__init__()


class PropertyYMLInvalidTag(InvalidPropertyYML):
    def __init__(self, path, version):
        self.path = path
        self.version = version
        self.issue = f"its 'version:' tag is set to {self.version}.  Only 2 is supported"
        super().__init__()


class RelationWrongType(CompilationException):
    def __init__(self, relation, expected_type, model=None):
        self.relation = relation
        self.expected_type = expected_type
        self.model = model
        super().__init__(self.get_message())

    def get_message(self) -> str:
        msg = (
            f"Trying to create {self.expected_type} {self.relation}, "
            f"but it currently exists as a {self.relation.type}. Either "
            f"drop {self.relation} manually, or run dbt with "
            "`--full-refresh` and dbt will drop it for you."
        )

        return msg


# These are placeholders to not immediately break app adapters utilizing these functions as exceptions.
# They will be removed in 1 (or 2?) versions.  Issue to be created to ensure it happens.
# TODO: add deprecation to functions
def warn(msg, node=None):  # type: ignore[no-redef] # noqa
    return warn(msg, node)


def missing_config(model, name) -> NoReturn:  # type: ignore[no-redef] # noqa
    missing_config(model, name)


def missing_materialization(model, adapter_type) -> NoReturn:  # type: ignore[no-redef] # noqa
    missing_materialization(model, adapter_type)


def missing_relation(relation, model=None) -> NoReturn:  # type: ignore[no-redef] # noqa
    missing_relation(relation, model)


def raise_ambiguous_alias(node_1, node_2, duped_name=None) -> NoReturn:  # type: ignore[no-redef] # noqa
    raise_ambiguous_alias(node_1, node_2, duped_name)


def raise_ambiguous_catalog_match(unique_id, match_1, match_2) -> NoReturn:  # type: ignore[no-redef] # noqa
    raise_ambiguous_catalog_match(unique_id, match_1, match_2)


def raise_cache_inconsistent(message) -> NoReturn:  # type: ignore[no-redef] # noqa
    raise_cache_inconsistent(message)


def raise_dataclass_not_dict(obj) -> NoReturn:  # type: ignore[no-redef] # noqa
    raise_dataclass_not_dict(obj)


# this is already used all over our code so for now can't do this until it's fully
# removed from this file.  otherwise casuses recurssion errors.
# def raise_compiler_error(msg, node=None) -> NoReturn:
#     raise_compiler_error(msg, node=None)


def raise_database_error(msg, node=None) -> NoReturn:  # type: ignore[no-redef] # noqa
    raise_database_error(msg, node)


def raise_dep_not_found(node, node_description, required_pkg) -> NoReturn:  # type: ignore[no-redef] # noqa
    raise_dep_not_found(node, node_description, required_pkg)


def raise_dependency_error(msg) -> NoReturn:  # type: ignore[no-redef] # noqa
    raise_dependency_error(msg)


def raise_duplicate_patch_name(patch_1, existing_patch_path) -> NoReturn:  # type: ignore[no-redef] # noqa
    raise_duplicate_patch_name(patch_1, existing_patch_path)


def raise_duplicate_resource_name(node_1, node_2) -> NoReturn:  # type: ignore[no-redef] # noqa
    raise_duplicate_resource_name(node_1, node_2)


def raise_invalid_property_yml_version(path, issue) -> NoReturn:  # type: ignore[no-redef] # noqa
    raise_invalid_property_yml_version(path, issue)


def raise_not_implemented(msg) -> NoReturn:  # type: ignore[no-redef] # noqa
    raise_not_implemented(msg)


def relation_wrong_type(relation, expected_type, model=None) -> NoReturn:  # type: ignore[no-redef] # noqa
    relation_wrong_type(relation, expected_type, model)
