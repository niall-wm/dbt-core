import click
import os
import sys
from typing import Optional

from dbt.cli.main import cli as dbt, deps
from dbt.tracking import track_run
from dbt.adapters.factory import adapter_management
from dbt.profiler import profiler
from dbt.config.runtime import load_project


def make_context(args, command=dbt) -> Optional[click.Context]:
    try:
        ctx = command.make_context(command.name, args)
    except click.exceptions.Exit:
        return None

    ctx.invoked_subcommand = ctx.protected_args[0] if ctx.protected_args else None
    ctx.obj = {}

    return ctx


# python core/dbt/cli/example.py
# python core/dbt/cli/example.py --version
# python core/dbt/cli/example.py deps --project-dir <project-dir-path>
# python core/dbt/cli/example.py run --project-dir <project-dir-path>
if __name__ == "__main__":
    cli_args = sys.argv[1:]

    # Use cli group to configure context + call arbitrary command
    ctx = make_context(cli_args)
    if ctx:
        dbt.invoke(ctx)

    # Bypass cli group context configuration entirely and invoke deps directly
    # Note: This only really works because of the prior global initializations (logging, tracking) from dbt.invoke(ctx)
    click.echo("\n`dbt deps` called")
    ctx_deps = make_context([], deps)
    assert ctx_deps is not None

    ctx_deps.with_resource(track_run(run_command="deps"))
    ctx_deps.with_resource(adapter_management())
    ctx_deps.with_resource(profiler(enable=True, outfile="output.profile"))
    profile_dir_override = os.path.expanduser("~/src/jaffle_shop")
    ctx_deps.obj["project"] = load_project(profile_dir_override, True, None, None)  # type: ignore
    deps.invoke(ctx_deps)
