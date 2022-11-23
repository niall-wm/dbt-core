import click
import sys

from dbt.cli.main import cli as dbt, deps


def make_context(args):
    try: 
        ctx = dbt.make_context('dbt', args)
    except click.exceptions.Exit:
        exit()

    ctx.invoked_subcommand = ctx.protected_args[0] if ctx.protected_args else None
    return ctx

# python core/dbt/cli/example.py
# python core/dbt/cli/example.py --version
# python core/dbt/cli/example.py deps --project-dir <project-dir-path>
if __name__ == "__main__":
    ctx = make_context(sys.argv[1:])
    dbt.invoke(ctx)
    
    # Skips group-level context setting
    # dbt.commands['deps'].invoke(ctx)
    # deps.invoke(ctx)
