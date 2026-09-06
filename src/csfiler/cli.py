"""Command line interface.

The commands map to the safety model: `scan` looks and proposes, `review`
lets a human decide, `apply` writes, `undo` reverses. Nothing writes to Drive
until `apply` is run against a batch that already exists on disk, so there is
always something to read before anything changes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .classify import DEFAULT_EFFORT, Classifier
from .config import load_config
from .journal import DEFAULT_DB_PATH, Journal
from .models import Disposition, Proposal
from .pipeline import Pipeline, summarize
from .storage import GoogleDriveStorage, LocalStorage

app = typer.Typer(
    add_completion=False,
    help="AI file renaming and filing for the Continuous Scale BizBox.",
)
console = Console()

DISPOSITION_STYLE = {
    Disposition.AUTO: "green",
    Disposition.REVIEW: "yellow",
    Disposition.UNIDENTIFIED: "red",
    Disposition.ERROR: "red",
    Disposition.DUPLICATE: "cyan",
    Disposition.EXCLUDED: "magenta",
    Disposition.SKIP_CONFORMING: "dim",
}


def _build_storage(source: str, root: Optional[str], config) -> object:
    if source == "local":
        if not root:
            raise typer.BadParameter("--root is required when --source local")
        return LocalStorage(root)
    return GoogleDriveStorage(
        root_folder_name=root or config.drive.root_folder_name,
        service_account_path=None,
    )


def _render(proposals: list[Proposal], show_all: bool) -> None:
    table = Table(show_lines=False, header_style="bold")
    table.add_column("Current name", overflow="fold", max_width=30)
    table.add_column("Proposed name", overflow="fold", max_width=38)
    table.add_column("Destination", overflow="fold", max_width=40)
    table.add_column("Conf", justify="right", width=5)
    table.add_column("Status", width=12)

    hidden = 0
    for p in proposals:
        if not show_all and p.disposition is Disposition.SKIP_CONFORMING:
            hidden += 1
            continue
        style = DISPOSITION_STYLE.get(p.disposition, "white")
        conf = f"{p.facts.confidence:.2f}" if p.facts else "-"
        detail = p.proposed_name or Text("—", style="dim")
        dest = p.proposed_path or Text(
            (p.blockers[0][:60] if p.blockers else "—"), style="dim italic"
        )
        table.add_row(p.file.name, detail, dest, conf, Text(p.disposition.value, style=style))

    console.print(table)
    if hidden:
        console.print(f"[dim]{hidden} already-conforming file(s) hidden. --all to show.[/dim]")


def _summary_line(proposals: list[Proposal]) -> None:
    counts = summarize(proposals)
    parts = [
        f"[{DISPOSITION_STYLE.get(Disposition(k), 'white')}]{v} {k}[/]"
        for k, v in sorted(counts.items())
    ]
    console.print("  ".join(parts) if parts else "[dim]nothing found[/dim]")


@app.command()
def scan(
    folder: str = typer.Argument("", help="Folder to scan, relative to the BizBox root."),
    source: str = typer.Option("drive", help="drive | local"),
    root: Optional[str] = typer.Option(None, help="Drive root folder name, or local root path."),
    config_path: Optional[Path] = typer.Option(None, "--config", help="Path to the YAML config."),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="Journal database path."),
    limit: Optional[int] = typer.Option(None, help="Stop after N files."),
    recursive: bool = typer.Option(True, help="Descend into subfolders."),
    effort: str = typer.Option(DEFAULT_EFFORT, help="Classifier effort: low|medium|high|xhigh|max"),
    show_all: bool = typer.Option(False, "--all", help="Include already-conforming files."),
    out: Optional[Path] = typer.Option(None, help="Write the proposals to a JSON file."),
) -> None:
    """Read a folder and propose a name and destination for every file.

    Writes nothing. Run this first, always.
    """
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    config = load_config(config_path)
    storage = _build_storage(source, root, config)

    with Journal(db) as journal:
        classifier = Classifier(
            config, corrections=journal.recent_corrections(), effort=effort
        )
        pipeline = Pipeline(config, storage, classifier, journal)

        with console.status("[bold]Reading files…") as status:
            def progress(phase: str, ref) -> None:
                status.update(f"[bold]{phase}[/] {ref.name}")

            batch_id, proposals = pipeline.scan(
                folder, recursive=recursive, limit=limit, progress=progress
            )

    console.print()
    _render(proposals, show_all)
    console.print()
    _summary_line(proposals)
    console.print(f"\n[dim]{classifier.usage_summary()}[/dim]")
    console.print(f"Batch [bold]{batch_id}[/bold]")

    auto = sum(1 for p in proposals if p.disposition is Disposition.AUTO)
    review = sum(1 for p in proposals if p.disposition is Disposition.REVIEW)
    if review:
        console.print(f"  [yellow]{review}[/] need review:  csfiler review --batch {batch_id}")
    if auto:
        console.print(f"  [green]{auto}[/] ready to apply: csfiler apply {batch_id}")

    if out:
        out.write_text(
            json.dumps([p.model_dump(mode="json") for p in proposals], indent=2),
            encoding="utf-8",
        )
        console.print(f"[dim]Proposals written to {out}[/dim]")


@app.command()
def apply(
    batch_id: str = typer.Argument(..., help="Batch id from a previous scan."),
    source: str = typer.Option("drive", help="drive | local"),
    root: Optional[str] = typer.Option(None, help="Drive root folder name, or local root path."),
    config_path: Optional[Path] = typer.Option(None, "--config"),
    db: Path = typer.Option(DEFAULT_DB_PATH),
    include_reviewed: bool = typer.Option(
        True, help="Also apply review-queue items a human approved."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Rename and move the files in a batch. This is the step that writes."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    config = load_config(config_path)
    storage = _build_storage(source, root, config)

    with Journal(db) as journal:
        loaded = journal.load_proposals(batch_id, ["auto", "review"])
        to_apply: list[Proposal] = []
        for _pid, proposal in loaded:
            if proposal.disposition is Disposition.AUTO:
                to_apply.append(proposal)
            elif include_reviewed and proposal.review_action in ("approved", "edited"):
                to_apply.append(proposal)

        actionable = [p for p in to_apply if p.is_actionable and (p.renames or p.moves)]
        if not actionable:
            console.print("[yellow]Nothing to apply in this batch.[/yellow]")
            raise typer.Exit(0)

        console.print(f"\nAbout to rename and file [bold]{len(actionable)}[/bold] file(s):\n")
        _render(actionable, show_all=True)

        if not yes:
            typer.confirm("\nApply these changes?", abort=True)

        classifier = Classifier(config, client=_NullClient())
        pipeline = Pipeline(config, storage, classifier, journal)
        with console.status("[bold]Applying…"):
            result = pipeline.apply(actionable, batch_id)

    console.print(
        f"\n[green]{result.applied} applied[/green], "
        f"{result.skipped} skipped, "
        f"[red]{result.failed} failed[/red]"
    )
    for name, err in result.errors:
        console.print(f"  [red]{name}[/red]: {err}")
    console.print(f"\n[dim]Reverse with: csfiler undo {batch_id}[/dim]")


@app.command()
def undo(
    batch_id: str = typer.Argument(..., help="Batch id to reverse."),
    source: str = typer.Option("drive", help="drive | local"),
    root: Optional[str] = typer.Option(None),
    config_path: Optional[Path] = typer.Option(None, "--config"),
    db: Path = typer.Option(DEFAULT_DB_PATH),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Put every file in a batch back to its original name and folder."""
    config = load_config(config_path)
    storage = _build_storage(source, root, config)

    with Journal(db) as journal:
        pending = journal.undoable_actions(batch_id)
        if not pending:
            console.print("[yellow]Nothing to undo in this batch.[/yellow]")
            raise typer.Exit(0)

        console.print(f"\nAbout to reverse [bold]{len(pending)}[/bold] change(s):\n")
        table = Table(header_style="bold")
        table.add_column("Now")
        table.add_column("Back to")
        for row in pending[:20]:
            table.add_row(row["new_name"], row["prior_name"])
        console.print(table)
        if len(pending) > 20:
            console.print(f"[dim]… and {len(pending) - 20} more[/dim]")

        if not yes:
            typer.confirm("\nReverse these changes?", abort=True)

        classifier = Classifier(config, client=_NullClient())
        pipeline = Pipeline(config, storage, classifier, journal)
        result = pipeline.undo(batch_id)

    console.print(f"\n[green]{result.applied} reversed[/green], [red]{result.failed} failed[/red]")
    for name, err in result.errors:
        console.print(f"  [red]{name}[/red]: {err}")


@app.command()
def review(
    batch: Optional[str] = typer.Option(None, help="Limit to one batch."),
    db: Path = typer.Option(DEFAULT_DB_PATH),
    config_path: Optional[Path] = typer.Option(None, "--config"),
    port: int = typer.Option(8765),
) -> None:
    """Open the review queue in a browser: approve, edit, or reject each proposal."""
    import uvicorn

    from .review import create_app

    console.print(f"Review queue at [bold]http://127.0.0.1:{port}[/bold]  (ctrl-c to stop)")
    uvicorn.run(
        create_app(db_path=db, config_path=config_path, batch_id=batch),
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )


@app.command()
def batches(
    db: Path = typer.Option(DEFAULT_DB_PATH),
    limit: int = typer.Option(15),
) -> None:
    """List recent scan and apply batches."""
    with Journal(db) as journal:
        rows = journal.list_batches(limit)
        if not rows:
            console.print("[dim]No batches yet.[/dim]")
            raise typer.Exit(0)

        table = Table(header_style="bold")
        table.add_column("Batch")
        table.add_column("Started")
        table.add_column("Source")
        table.add_column("Result", overflow="fold")
        table.add_column("Undoable", justify="right")
        for row in rows:
            summary = json.loads(row["summary"]) if row["summary"] else {}
            pretty = ", ".join(f"{v} {k}" for k, v in sorted(summary.items()))
            undoable = len(journal.undoable_actions(row["id"]))
            table.add_row(
                row["id"],
                row["started_at"][:19].replace("T", " "),
                row["source"],
                pretty or "-",
                str(undoable) if undoable else "-",
            )
        console.print(table)


@app.command()
def doctor(
    source: str = typer.Option("drive", help="drive | local"),
    root: Optional[str] = typer.Option(None),
    config_path: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Check that the config, the API key, and Drive access all work."""
    ok = True

    try:
        config = load_config(config_path)
        console.print(
            f"[green]✓[/green] Config loaded: {len(config.document_types)} document types, "
            f"{len(config.all_entities())} known counterparties"
        )
    except Exception as exc:
        console.print(f"[red]✗[/red] Config: {exc}")
        raise typer.Exit(1)

    import os

    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        console.print("[green]✓[/green] Anthropic credentials present in the environment")
    else:
        console.print(
            "[yellow]![/yellow] No ANTHROPIC_API_KEY set. The SDK will fall back to an "
            "`ant auth login` profile if one exists."
        )

    try:
        storage = _build_storage(source, root, config)
        if source == "drive":
            storage.root_id()  # type: ignore[attr-defined]
            console.print(
                f"[green]✓[/green] Drive reachable; found root "
                f"{root or config.drive.root_folder_name!r}"
            )
        else:
            console.print(f"[green]✓[/green] Local root {root}")
    except Exception as exc:
        ok = False
        console.print(f"[red]✗[/red] Storage: {exc}")

    raise typer.Exit(0 if ok else 1)


class _NullClient:
    """Stands in for the API client on commands that never classify."""

    class _Messages:
        def parse(self, **_kwargs):
            raise RuntimeError("This command does not classify documents.")

    messages = _Messages()


if __name__ == "__main__":
    app()
