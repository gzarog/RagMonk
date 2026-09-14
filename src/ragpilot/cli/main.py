"""Root Typer application wiring every RAGpilot subcommand."""

from __future__ import annotations

import typer

from ragpilot.cli import (
    ask,
    backup,
    callees,
    callers,
    config_cmd,
    daemon,
    docs,
    doctor,
    explore,
    impact,
    index,
    init,
    install_agent,
    link,
    rebuild,
    references,
    restore,
    search,
    serve,
    source,
    status,
    symbol,
    uninstall,
    update,
    upgrade,
    vectors,
    version_cmd,
    watch,
)

app = typer.Typer(
    name="ragpilot",
    no_args_is_help=True,
    add_completion=False,
    help="Local-first knowledge compiler and retrieval engine.",
)

app.add_typer(source.app, name="source")
app.add_typer(config_cmd.app, name="config")
app.add_typer(link.app, name="link")
app.add_typer(daemon.app, name="daemon")
app.add_typer(vectors.app, name="vectors")
app.add_typer(update.app, name="update")

app.command("init", help="Bootstrap the RAGpilot runtime directory.")(init.init)
app.command("index", help="Scan sources and process pending files.")(index.index)
app.command("status", help="Show indexing status.")(status.status)
app.command("doctor", help="Run health checks.")(doctor.doctor)
app.command("health", help="Show a condensed health summary.")(doctor.health)
app.command("version", help="Show the RAGpilot version.")(version_cmd.version)
app.command("symbol", help="Look up a code symbol by name.")(symbol.symbol)
app.command("callers", help="Show entities that call the given symbol.")(callers.callers)
app.command("callees", help="Show entities the given symbol calls.")(callees.callees)
app.command("references", help="Show all edges touching the given symbol.")(references.references)
app.command("docs", help="List indexed documents.")(docs.docs)
app.command("search", help="Lexical search across code and documents.")(search.search)
app.command("impact", help="Show blast-radius impact analysis for a symbol.")(impact.impact)
app.command(
    "explore", help="Explore a query using all deterministic retrieval strategies."
)(explore.explore)
app.command("serve", help="Start RAGpilot as a server (MCP over stdio).")(serve.serve)
app.command("watch", help="Run the indexing daemon in the foreground.")(watch.watch)
app.command(
    "install-agent", help="Print (and optionally write) the MCP client config snippet."
)(install_agent.install_agent)
app.command("backup", help="Create a restorable backup archive of RAGpilot's databases.")(
    backup.backup
)
app.command("restore", help="Restore RAGpilot's databases from a backup archive.")(
    restore.restore
)
app.command("rebuild", help="Wipe and re-index one or every source's derived knowledge.")(
    rebuild.rebuild
)
app.command("upgrade", help="Apply pending schema migrations, backing up first if needed.")(
    upgrade.upgrade
)
app.command("ask", help="Ask a question, answered by an AI provider grounded in real evidence.")(
    ask.ask
)
app.command(
    "uninstall", help="Remove the RAGpilot application and, by default, all of its data."
)(uninstall.uninstall)


if __name__ == "__main__":
    app()
