"""Command-line interface: one subcommand per pipeline stage."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import click

from . import db, export, policy, voice_export
from .config import Config, config_file_path, load_config, write_default_config
from .ingest import contacts, store
from .logging_setup import setup_logging
from .rules import engine


@click.group()
@click.option(
    "--config",
    "config_path",
    type=click.Path(),
    default=None,
    help="Path to config.toml (default: ./config.toml or $EMAIL_CLEANER_CONFIG).",
)
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
@click.pass_context
def cli(ctx: click.Context, config_path: str | None, verbose: bool) -> None:
    """Local-LLM-powered Gmail cleanup: parse Takeout MBOX, classify locally,
    review, then apply approved actions via the Gmail API (trash, never delete)."""
    setup_logging(verbose)
    ctx.obj = load_config(config_path)


@cli.command()
@click.option(
    "--email",
    "emails",
    multiple=True,
    help="Your own address(es), for Sent-mail/known-contact detection.",
)
@click.pass_obj
def init(cfg: Config, emails: tuple[str, ...]) -> None:
    """Create config.toml, data/secrets dirs, and the SQLite schema."""
    cfg_path = config_file_path()
    if cfg_path.exists():
        click.echo(f"Config already exists: {cfg_path}")
    else:
        write_default_config(cfg_path, tuple(e.strip().lower() for e in emails))
        click.echo(f"Wrote {cfg_path}" + ("" if emails else " — edit user_addresses!"))

    cfg = load_config()  # reload in case we just created the file
    for directory in (cfg.db_path.parent, cfg.credentials_path.parent):
        directory.mkdir(parents=True, exist_ok=True)
    conn = db.open_db(cfg.db_path)
    conn.close()
    click.echo(f"Database ready: {cfg.db_path}")
    click.echo(
        f"Next: put your Takeout export at {cfg.mbox_path} and run `email-cleaner ingest`."
    )


@cli.command()
@click.argument(
    "mbox_path", type=click.Path(exists=True, dir_okay=False), required=False
)
@click.option(
    "--limit", type=int, default=None, help="Only ingest the first N messages."
)
@click.option("--skip-contacts", is_flag=True, help="Skip known-contact derivation.")
@click.pass_obj
def ingest(
    cfg: Config, mbox_path: str | None, limit: int | None, skip_contacts: bool
) -> None:
    """Stream-parse the MBOX into SQLite (idempotent; re-runs skip duplicates)."""
    path = Path(mbox_path) if mbox_path else cfg.mbox_path
    if not path.is_file():
        raise click.ClickException(f"MBOX not found: {path}")

    conn = db.open_db(cfg.db_path)
    click.echo(f"Ingesting {path} ...")
    stats = store.ingest_mbox(
        conn,
        path,
        limit=limit,
        progress=lambda s: click.echo(
            f"  {s.seen} seen, {s.inserted} inserted", nl=True
        ),
    )
    click.echo(
        f"Done: {stats.seen} messages seen, {stats.inserted} inserted, "
        f"{stats.skipped} duplicates skipped."
    )

    if not skip_contacts:
        n = contacts.derive_contacts(conn, cfg.user_addresses)
        if cfg.user_addresses:
            click.echo(f"Known contacts derived from Sent mail: {n}")
            if n == 0:
                click.secho(
                    "WARNING: user_addresses is set but 0 contacts were derived — "
                    "the known-contact protection will not fire. Check that the "
                    "configured addresses match your Sent mail's From addresses "
                    "(most frequent senders in this mbox):",
                    fg="yellow",
                )
                for addr, count in contacts.suggest_user_addresses(conn):
                    click.echo(f"  {addr}  ({count} messages)")
        else:
            click.echo("user_addresses is empty — most frequent senders in this mbox:")
            for addr, count in contacts.suggest_user_addresses(conn):
                click.echo(f"  {addr}  ({count} messages)")
            click.echo(
                "Add yours to [rules].user_addresses and re-run "
                "`email-cleaner ingest --limit 0` to derive contacts."
            )
    conn.close()


@cli.command()
@click.option(
    "--reset",
    is_flag=True,
    help="Clear previous rule/LLM results (pending rows only) and re-evaluate.",
)
@click.pass_obj
def rules(cfg: Config, reset: bool) -> None:
    """Run the deterministic rules engine (always before classify)."""
    conn = db.open_db(cfg.db_path)
    ctx = engine.load_context(conn)
    click.echo(f"Evaluating rules ({len(ctx.known_contacts)} known contacts) ...")
    counts = engine.run_rules(conn, ctx, reset=reset)
    for label, n in counts.most_common():
        click.echo(f"  {label:24} {n}")
    conn.close()


@cli.command()
@click.option("--limit", type=int, default=None, help="Classify at most N messages.")
@click.option("--model", default=None, help="Override the Ollama model tag.")
@click.option(
    "--batch-size",
    type=click.IntRange(min=1),
    default=None,
    help="Commit every N classifications.",
)
@click.option(
    "--concurrency",
    type=click.IntRange(min=1),
    default=None,
    help="Requests in flight at once (pair with OLLAMA_NUM_PARALLEL on the host).",
)
@click.pass_obj
def classify(
    cfg: Config,
    limit: int | None,
    model: str | None,
    batch_size: int | None,
    concurrency: int | None,
) -> None:
    """Classify ambiguous + delete-candidate messages with the local LLM."""
    from tqdm import tqdm
    from tqdm.contrib.logging import logging_redirect_tqdm

    from .llm import chain as chain_mod
    from .llm import classifier

    if model:
        cfg = dataclasses.replace(cfg, ollama_model=model)
    if batch_size is not None:
        cfg = dataclasses.replace(cfg, llm_batch_size=batch_size)
    if concurrency is not None:
        cfg = dataclasses.replace(cfg, llm_concurrency=concurrency)

    chain_mod.check_model_available(cfg)  # fails fast if Ollama is unreachable
    chain = chain_mod.build_classifier_chain(cfg)
    conn = db.open_db(cfg.db_path)

    click.echo(
        f"Classifying with {cfg.ollama_model} at {cfg.ollama_url} "
        f"({cfg.llm_concurrency} requests in flight) ..."
    )
    try:
        with tqdm(desc="Classifying", unit="msg") as bar, logging_redirect_tqdm():

            def progress(s: classifier.ClassifyStats, total: int) -> None:
                bar.total = total
                bar.n = s.processed + s.failed
                bar.set_postfix(failed=s.failed, **dict(s.by_action))

            stats = classifier.classify_messages(
                conn, cfg, chain, limit=limit, progress=progress
            )
    except KeyboardInterrupt:
        conn.close()  # finished work was committed before the interrupt surfaced
        click.echo(
            "\nInterrupted — completed classifications are saved; "
            "re-run `email-cleaner classify` to resume."
        )
        # In-flight Ollama requests can't be cancelled mid-generation; exit
        # without joining their abandoned worker threads (interpreter exit
        # would block on them). Dropping the connections makes Ollama abort
        # the generations server-side.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)  # 128 + SIGINT
    click.echo(f"Done: {stats.processed} classified, {stats.failed} failed.")
    click.echo("Next: `email-cleaner policy`, then `email-cleaner review`.")
    conn.close()


@cli.command("policy")
@click.pass_obj
def policy_cmd(cfg: Config) -> None:
    """Apply the auto-trash/auto-archive policy gates (re-runnable after tuning)."""
    conn = db.open_db(cfg.db_path)
    result = policy.apply_policy(conn, cfg)
    click.echo(f"Auto-approved for trash: {result['auto_approved']}")
    click.echo(f"Auto-approved for archive: {result['auto_archived']}")
    click.echo(
        f"Trash proposals left for human review: {result['pending_trash_for_review']}"
    )
    click.echo(
        f"Archive proposals left for human review: {result['pending_archive_for_review']}"
    )
    conn.close()


@cli.command()
@click.pass_obj
def review(cfg: Config) -> None:
    """Launch the Streamlit review UI (port 8501, auto-forwarded by VS Code)."""
    app_path = Path(__file__).parent / "review" / "app.py"
    raise SystemExit(
        subprocess.call(
            [sys.executable, "-m", "streamlit", "run", str(app_path)],
        )
    )


@cli.command("export")
@click.argument("out_path", type=click.Path(dir_okay=False))
@click.pass_obj
def export_cmd(cfg: Config, out_path: str) -> None:
    """Export the approved action table to CSV."""
    conn = db.open_db(cfg.db_path)
    n = export.export_actions(conn, out_path)
    click.echo(f"Wrote {n} approved actions to {out_path}")
    conn.close()


@cli.command("voice-export")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False),
    default=None,
    help="Output directory (default: [voice].out_dir).",
)
@click.option(
    "--no-trash",
    is_flag=True,
    help="Export to disk only; don't stage the messages for trash.",
)
@click.option(
    "--mbox",
    "mbox_path",
    type=click.Path(dir_okay=False),
    default=None,
    help="Source MBOX for recovering attachment bytes (default: the ingested one).",
)
@click.option(
    "--no-attachments",
    is_flag=True,
    help="Skip recovering MMS images from the MBOX (text export only).",
)
@click.pass_obj
def voice_export_cmd(
    cfg: Config,
    out_dir: str | None,
    no_trash: bool,
    mbox_path: str | None,
    no_attachments: bool,
) -> None:
    """Export Google Voice SMS / call logs to disk, then stage them for trash.

    Writes JSONL + per-contact transcripts + a calls CSV, recovers MMS images
    from the source MBOX, then (unless --no-trash) stages the exported messages
    DELETE_CANDIDATE for the normal review/apply flow. Re-runnable; the files
    are rewritten in full each time."""
    from tqdm import tqdm
    from tqdm.contrib.logging import logging_redirect_tqdm

    out = Path(out_dir) if out_dir else cfg.voice_out_dir
    conn = db.open_db(cfg.db_path)
    # Recovering attachments re-reads the whole source MBOX — the slow part.
    with tqdm(desc="Scanning MBOX", unit="msg", disable=no_attachments) as bar:

        def progress(scanned: int, total: int) -> None:
            bar.total = total
            bar.n = scanned
            bar.refresh()

        with logging_redirect_tqdm():
            stats = voice_export.export_voice(
                conn,
                out,
                set_disposition=cfg.voice_trash_after_export and not no_trash,
                include_attachments=not no_attachments,
                mbox_path=mbox_path,
                progress=progress,
            )
    conn.close()
    click.echo(
        f"Exported {stats.sms} SMS, {stats.calls} calls, {stats.voicemails} "
        f"voicemails across {stats.contacts} contacts to {stats.out_dir}"
    )
    if stats.attachments_saved or stats.attachments_skipped:
        click.echo(
            f"Attachments: {stats.attachments_saved} recovered, "
            f"{stats.attachments_skipped} not recovered"
            + (
                f" (source mbox not found: {stats.mbox_path or 'unknown'})"
                if stats.attachments_skipped and not stats.attachments_saved
                else ""
            )
        )
    if stats.staged_for_trash:
        click.echo(
            f"Staged {stats.staged_for_trash} messages for trash "
            "(pending review — run `email-cleaner review`, then `apply`)."
        )
    elif no_trash or not cfg.voice_trash_after_export:
        click.echo("Backup only — no messages were staged for trash.")


@cli.command()
@click.pass_obj
def auth(cfg: Config) -> None:
    """Run the one-time Gmail OAuth flow and cache the token."""
    from .gmail import auth as gmail_auth

    gmail_auth.get_credentials(cfg)
    click.echo(f"Authenticated; token cached at {cfg.token_path}")


@cli.command()
@click.option(
    "--execute",
    is_flag=True,
    help="Actually mutate Gmail. Without this flag, apply is a dry run.",
)
@click.option("--limit", type=int, default=None, help="Process at most N messages.")
@click.pass_obj
def apply(cfg: Config, execute: bool, limit: int | None) -> None:
    """Reconcile approved actions against live Gmail and trash/archive them.

    Dry-run by default: reconciles (read-only) and logs what WOULD happen."""
    from .gmail import auth as gmail_auth
    from .gmail import runner

    if execute:
        click.confirm(
            "This will move messages to Trash / archive them in your LIVE Gmail. Continue?",
            abort=True,
        )

    conn = db.open_db(cfg.db_path)
    service = gmail_auth.get_service(cfg)
    mode = "EXECUTING" if execute else "DRY RUN"
    click.echo(f"[{mode}] applying approved actions ...")
    stats = runner.apply_actions(
        conn,
        cfg,
        service,
        execute=execute,
        limit=limit,
        progress=lambda s: click.echo(
            f"  {s.examined} examined, {s.succeeded} ok, "
            f"{s.skipped} skipped, {s.errors} errors"
        ),
    )
    click.echo(
        f"[{mode}] done: {stats.examined} examined, {stats.succeeded} succeeded, "
        f"{stats.skipped} skipped, {stats.errors} errors."
    )
    for reason, n in stats.skip_reasons.items():
        click.echo(f"  skipped ({n}): {reason}")
    if not execute:
        click.echo("Inspect the `actions` audit table, then re-run with --execute.")
    conn.close()


@cli.command()
@click.pass_obj
def status(cfg: Config) -> None:
    """Show pipeline progress counts."""
    if not cfg.db_path.is_file():
        raise click.ClickException(
            f"No database at {cfg.db_path} — run `email-cleaner init`."
        )
    conn = db.open_db(cfg.db_path)

    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    click.echo(f"Messages: {total}")

    click.echo("\nStaged labels:")
    for row in conn.execute(
        "SELECT COALESCE(staged_label, '(not yet ruled)') AS l, COUNT(*) AS n "
        "FROM messages GROUP BY staged_label ORDER BY n DESC"
    ):
        click.echo(f"  {row['l']:24} {row['n']}")

    click.echo("\nReview status / proposed action:")
    for row in conn.execute(
        "SELECT review_status, COALESCE(proposed_action,'-') AS a, COUNT(*) AS n "
        "FROM messages GROUP BY review_status, proposed_action ORDER BY n DESC"
    ):
        click.echo(f"  {row['review_status']:14} {row['a']:8} {row['n']}")

    unclassified = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE review_status='pending' AND ai_confidence IS NULL "
        "AND (staged_label='NEEDS_REVIEW' AND classified_by IS NULL "
        "     OR staged_label='DELETE_CANDIDATE')"
    ).fetchone()[0]
    n_contacts = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    n_actions = conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    click.echo(f"\nAwaiting LLM classification: {unclassified}")
    click.echo(f"Known contacts: {n_contacts};  audit log rows: {n_actions}")
    conn.close()


def main() -> None:
    cli()
