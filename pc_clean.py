#!/usr/bin/env python3
"""pc-clean — Storage & resource manager for developers."""

import os
import sys
import shutil
import subprocess
import json
import hashlib
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.columns import Columns
    from rich.text import Text
    from rich.prompt import Prompt, Confirm
    from rich.progress import track
    from rich import box
except ImportError:
    print("Installation de 'rich'...")
    subprocess.run([sys.executable, "-m", "pip", "install", "rich", "-q"])
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.columns import Columns
    from rich.text import Text
    from rich.prompt import Prompt, Confirm
    from rich.progress import track
    from rich import box

console = Console()
HOME = Path.home()


# ─── Utilities ───────────────────────────────────────────────────────────────

def run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except (subprocess.TimeoutExpired, Exception):
        return ""


def fmt_size(size_bytes):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def dir_size(path):
    total = 0
    try:
        for entry in os.scandir(path):
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                elif entry.is_dir(follow_symlinks=False):
                    total += dir_size(entry.path)
            except (PermissionError, OSError):
                pass
    except (PermissionError, OSError):
        pass
    return total


def severity_color(pct):
    if pct >= 85:
        return "bold red"
    elif pct >= 70:
        return "yellow"
    return "green"


# ─── 1. Disk Overview ───────────────────────────────────────────────────────

def show_disk_overview():
    console.print("\n[bold cyan]══ DISQUES ══[/bold cyan]\n")
    table = Table(box=box.ROUNDED, show_header=True, header_style="bold white")
    table.add_column("Disque", style="cyan")
    table.add_column("Total", justify="right")
    table.add_column("Utilisé", justify="right")
    table.add_column("Libre", justify="right")
    table.add_column("Usage", justify="right")
    table.add_column("Barre", min_width=20)

    output = run("df -h --output=source,size,used,avail,pcent,target -x tmpfs -x devtmpfs -x efivarfs -x squashfs 2>/dev/null")
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6 and parts[0].startswith("/dev/"):
            dev, total, used, avail, pct_str, mount = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
            pct = int(pct_str.replace("%", ""))
            filled = int(pct / 5)
            bar = f"[{severity_color(pct)}]{'█' * filled}{'░' * (20 - filled)}[/{severity_color(pct)}]"
            label = mount if mount != "/" else "/ (SSD)"
            table.add_row(label, total, used, avail, f"[{severity_color(pct)}]{pct}%[/{severity_color(pct)}]", bar)

    console.print(table)

    # RAM & Swap
    mem = run("free -h --si | grep -E 'Mem|Swap'")
    if mem:
        console.print(f"\n[dim]{mem}[/dim]")


# ─── 2. Home Directory Breakdown ────────────────────────────────────────────

def show_home_breakdown():
    console.print("\n[bold cyan]══ REPARTITION HOME ══[/bold cyan]\n")
    table = Table(box=box.ROUNDED, show_header=True, header_style="bold white")
    table.add_column("Dossier", style="cyan")
    table.add_column("Taille", justify="right", style="bold")
    table.add_column("Type", style="dim")
    table.add_column("Action", style="yellow")

    entries = []
    for item in HOME.iterdir():
        try:
            if item.is_symlink():
                continue
            if item.is_file():
                size = item.stat().st_size
            elif item.is_dir():
                size = dir_size(str(item))
            else:
                continue
            entries.append((item.name, size))
        except (PermissionError, OSError):
            pass

    # Include hidden dirs
    for item in HOME.iterdir():
        if item.name.startswith(".") and item.is_dir() and not item.is_symlink():
            try:
                size = dir_size(str(item))
                if size > 50 * 1024 * 1024:  # >50MB
                    entries.append((item.name, size))
            except (PermissionError, OSError):
                pass

    # Deduplicate
    seen = set()
    unique = []
    for name, size in entries:
        if name not in seen:
            seen.add(name)
            unique.append((name, size))

    unique.sort(key=lambda x: x[1], reverse=True)

    categories = {
        ".local": ("Cache/Système", "Vérifier corbeille"),
        "Downloads": ("Téléchargements", "Nettoyer doublons"),
        ".cache": ("Cache", "Nettoyable"),
        ".config": ("Configuration", "Ne pas toucher"),
        "Desktop": ("Bureau", "Nettoyer .zip doublons"),
        "snap": ("Snap packages", "Système"),
        "flutter": ("SDK Flutter", "Garder si utilisé"),
        ".gradle": ("Cache Gradle", "Nettoyable"),
        "Android": ("SDK Android", "Garder si dev mobile"),
        "Pictures": ("Images", "Trier"),
        ".docker": ("Docker config", "Ne pas toucher"),
    }

    for name, size in unique[:20]:
        if size < 10 * 1024 * 1024:  # Skip < 10MB
            continue
        cat, action = categories.get(name, ("Projet/Fichier", "—"))
        table.add_row(name, fmt_size(size), cat, action)

    console.print(table)


# ─── 3. Trash Analysis ──────────────────────────────────────────────────────

def show_trash():
    trash_path = HOME / ".local" / "share" / "Trash"
    if not trash_path.exists():
        return 0

    size = dir_size(str(trash_path))
    if size > 100 * 1024 * 1024:  # > 100MB
        console.print(f"\n[bold red]⚠  CORBEILLE : {fmt_size(size)}[/bold red]")
        console.print(f"   Chemin: {trash_path}")
    return size


def clean_trash():
    trash_path = HOME / ".local" / "share" / "Trash"
    if not trash_path.exists():
        console.print("[green]Corbeille déjà vide.[/green]")
        return

    size = dir_size(str(trash_path))
    if size < 1024:
        console.print("[green]Corbeille déjà vide.[/green]")
        return

    console.print(f"\n[yellow]Corbeille : {fmt_size(size)}[/yellow]")
    if Confirm.ask("Vider la corbeille ?"):
        for sub in ["files", "info", "expunged"]:
            p = trash_path / sub
            if p.exists():
                shutil.rmtree(str(p), ignore_errors=True)
                p.mkdir(exist_ok=True)
        console.print(f"[green]✓ {fmt_size(size)} libérés ![/green]")


# ─── 4. Docker Analysis ─────────────────────────────────────────────────────

def docker_available():
    return run("docker info 2>/dev/null") != ""


def show_docker():
    if not docker_available():
        console.print("\n[dim]Docker non disponible.[/dim]")
        return

    console.print("\n[bold cyan]══ DOCKER ══[/bold cyan]\n")

    # System df
    df_out = run("docker system df --format '{{.Type}}\t{{.TotalCount}}\t{{.Size}}\t{{.Reclaimable}}'")
    table = Table(box=box.ROUNDED, title="Espace Docker", header_style="bold white")
    table.add_column("Type", style="cyan")
    table.add_column("Nombre", justify="right")
    table.add_column("Taille", justify="right", style="bold")
    table.add_column("Récupérable", justify="right", style="green")

    for line in df_out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            table.add_row(*parts[:4])
    console.print(table)

    # Stopped containers
    stopped = run("docker ps -a --filter 'status=exited' --format '{{.Names}}\t{{.Status}}\t{{.Size}}'")
    if stopped:
        console.print("\n[bold yellow]Containers arrêtés :[/bold yellow]")
        ct = Table(box=box.SIMPLE, header_style="bold")
        ct.add_column("Nom", style="cyan")
        ct.add_column("Statut", style="red")
        ct.add_column("Taille", justify="right")
        for line in stopped.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                ct.add_row(*parts[:3])
        console.print(ct)

    # Dangling images
    dangling = run("docker images -f 'dangling=true' --format '{{.ID}}\t{{.Size}}'")
    if dangling:
        count = len(dangling.splitlines())
        console.print(f"\n[yellow]⚠  {count} images orphelines (dangling)[/yellow]")

    # Large images
    console.print("\n[bold]Images les plus lourdes :[/bold]")
    images = run("docker images --format '{{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}'")
    it = Table(box=box.SIMPLE, header_style="bold")
    it.add_column("Image", style="cyan")
    it.add_column("Taille", justify="right", style="bold")
    it.add_column("Créée", style="dim")
    for line in images.splitlines()[:15]:
        parts = line.split("\t")
        if len(parts) >= 3:
            it.add_row(*parts[:3])
    console.print(it)


def clean_docker():
    if not docker_available():
        console.print("[dim]Docker non disponible.[/dim]")
        return

    console.print("\n[bold cyan]Nettoyage Docker[/bold cyan]\n")

    options = [
        ("1", "Containers arrêtés seulement", "docker container prune -f"),
        ("2", "Containers + images non utilisées", "docker system prune -a -f"),
        ("3", "TOUT (containers + images + volumes + cache)", "docker system prune -a --volumes -f"),
        ("4", "Build cache seulement", "docker builder prune -a -f"),
    ]

    for key, desc, _ in options:
        console.print(f"  [{key}] {desc}")
    console.print("  [0] Annuler")

    choice = Prompt.ask("\nChoix", choices=["0", "1", "2", "3", "4"], default="0")

    if choice == "0":
        return

    cmd = options[int(choice) - 1][2]
    console.print(f"\n[yellow]Exécution : {cmd}[/yellow]")
    if Confirm.ask("Confirmer ?"):
        result = run(cmd, timeout=120)
        console.print(f"[green]✓ Terminé ![/green]\n{result}")


# ─── 5. Port Scanner ────────────────────────────────────────────────────────

def show_ports():
    console.print("\n[bold cyan]══ PORTS EN ECOUTE ══[/bold cyan]\n")

    output = run("ss -tlnp 2>/dev/null")
    if not output:
        output = run("netstat -tlnp 2>/dev/null")

    table = Table(box=box.ROUNDED, header_style="bold white")
    table.add_column("Port", style="bold cyan", justify="right")
    table.add_column("Adresse", style="dim")
    table.add_column("Process", style="yellow")
    table.add_column("Type")

    system_ports = {22: "SSH", 53: "DNS", 631: "Imprimante (CUPS)", 5900: "VNC"}
    seen_ports = set()

    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue

        addr = parts[3]
        if ":" not in addr:
            continue

        port_str = addr.rsplit(":", 1)[-1]
        try:
            port = int(port_str)
        except ValueError:
            continue

        if port in seen_ports:
            continue
        seen_ports.add(port)

        bind_addr = addr.rsplit(":", 1)[0]
        process = ""
        if "users:" in line:
            try:
                proc_part = line.split("users:")[1]
                process = proc_part.split('"')[1]
            except (IndexError, ValueError):
                process = "?"

        if not process:
            # Try to identify via docker
            docker_port = run(f"docker ps --filter 'publish={port}' --format '{{{{.Names}}}}' 2>/dev/null")
            if docker_port:
                process = f"docker:{docker_port}"

        if port in system_ports:
            ptype = f"[dim]Système ({system_ports[port]})[/dim]"
        elif process and "docker" in process.lower():
            ptype = "[blue]Docker[/blue]"
        else:
            ptype = "[yellow]Application[/yellow]"

        table.add_row(str(port), bind_addr, process or "—", ptype)

    console.print(table)


# ─── 6. Downloads Analyzer ──────────────────────────────────────────────────

def show_downloads():
    dl = HOME / "Downloads"
    if not dl.exists():
        return

    console.print("\n[bold cyan]══ DOWNLOADS ══[/bold cyan]\n")

    # Find large files
    files = []
    for item in dl.iterdir():
        try:
            if item.is_file():
                size = item.stat().st_size
            elif item.is_dir():
                size = dir_size(str(item))
            else:
                continue
            files.append((item, size))
        except (PermissionError, OSError):
            pass

    files.sort(key=lambda x: x[1], reverse=True)

    # Detect duplicates: zip + extracted folder, and (2) copies
    names = {f.name: f for f, _ in files}
    duplicates = []
    seen_dup = set()
    for f, size in files:
        if f.name in seen_dup:
            continue
        if f.is_file() and f.suffix == ".zip":
            folder_name = f.stem
            if folder_name in names and names[folder_name].is_dir():
                duplicates.append((f, size, f"Doublon de dossier '{folder_name}/'"))
                seen_dup.add(f.name)
        elif f.is_file() and f.name.endswith(".tar.gz"):
            folder_name = f.name.replace(".tar.gz", "")
            if folder_name in names and names[folder_name].is_dir():
                duplicates.append((f, size, f"Doublon de dossier '{folder_name}/'"))
                seen_dup.add(f.name)
        elif f.name.endswith(")") and " (" in f.name:
            # "file (2).ext" or "folder (2)" pattern
            if f.is_file() and f.suffix:
                base = f.stem.rsplit(" (", 1)[0] + f.suffix
            else:
                base = f.name.rsplit(" (", 1)[0]
            if base in names:
                duplicates.append((f, size, f"Copie de '{base}'"))
                seen_dup.add(f.name)

    if duplicates:
        console.print("[bold red]Doublons détectés :[/bold red]")
        dt = Table(box=box.SIMPLE, header_style="bold")
        dt.add_column("Fichier", style="red")
        dt.add_column("Taille", justify="right")
        dt.add_column("Raison")
        total_dup = 0
        for f, size, reason in duplicates:
            dt.add_row(f.name, fmt_size(size), reason)
            total_dup += size
        console.print(dt)
        console.print(f"[bold red]Total doublons : {fmt_size(total_dup)}[/bold red]\n")

    # Large files table
    console.print("[bold]Fichiers les plus volumineux :[/bold]")
    ft = Table(box=box.SIMPLE, header_style="bold")
    ft.add_column("#", style="dim", justify="right")
    ft.add_column("Fichier", style="cyan")
    ft.add_column("Taille", justify="right", style="bold")
    ft.add_column("Type")
    ft.add_column("Modifié", style="dim")

    for i, (f, size) in enumerate(files[:20], 1):
        if size < 1024 * 1024:  # Skip < 1MB
            break
        mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
        ftype = "Dossier" if f.is_dir() else f.suffix or "—"
        ft.add_row(str(i), f.name[:60], fmt_size(size), ftype, mtime)

    console.print(ft)


def clean_downloads():
    dl = HOME / "Downloads"
    if not dl.exists():
        return

    files = []
    for item in dl.iterdir():
        try:
            if item.is_file():
                size = item.stat().st_size
            elif item.is_dir():
                size = dir_size(str(item))
            else:
                continue
            files.append((item, size))
        except (PermissionError, OSError):
            pass

    names = {f.name: f for f, _ in files}
    duplicates = []
    seen_dup = set()

    for f, size in files:
        if f.name in seen_dup:
            continue
        if f.is_file() and f.suffix == ".zip":
            folder_name = f.stem
            if folder_name in names and names[folder_name].is_dir():
                duplicates.append((f, size))
                seen_dup.add(f.name)
        elif f.name.endswith(")") and " (" in f.name:
            if f.is_file() and f.suffix:
                base = f.stem.rsplit(" (", 1)[0] + f.suffix
            else:
                base = f.name.rsplit(" (", 1)[0]
            if base in names:
                duplicates.append((f, size))
                seen_dup.add(f.name)

    if not duplicates:
        console.print("[green]Aucun doublon détecté dans Downloads.[/green]")
        return

    console.print(f"\n[yellow]{len(duplicates)} doublons trouvés :[/yellow]\n")
    total = 0
    for f, size in duplicates:
        console.print(f"  [red]✗[/red] {f.name} ({fmt_size(size)})")
        total += size

    console.print(f"\n[bold]Total : {fmt_size(total)}[/bold]")

    if Confirm.ask("\nSupprimer ces doublons ?"):
        for f, size in duplicates:
            try:
                if f.is_dir():
                    shutil.rmtree(str(f))
                else:
                    f.unlink()
                console.print(f"  [green]✓[/green] {f.name}")
            except Exception as e:
                console.print(f"  [red]✗ Erreur: {e}[/red]")
        console.print(f"\n[green]✓ {fmt_size(total)} libérés ![/green]")


# ─── 7. Cache Analyzer ──────────────────────────────────────────────────────

def show_caches():
    console.print("\n[bold cyan]══ CACHES ══[/bold cyan]\n")

    cache_dir = HOME / ".cache"
    if not cache_dir.exists():
        return

    caches = []
    for item in cache_dir.iterdir():
        if item.is_dir():
            try:
                size = dir_size(str(item))
                if size > 10 * 1024 * 1024:  # > 10MB
                    caches.append((item.name, size))
            except (PermissionError, OSError):
                pass

    caches.sort(key=lambda x: x[1], reverse=True)

    safe_to_clean = {"pip", "ms-playwright", "electron", "thumbnails", "yarn", "npm", "pnpm", "nuget"}
    careful = {"google-chrome", "mozilla", "chromium"}

    table = Table(box=box.ROUNDED, header_style="bold white")
    table.add_column("Cache", style="cyan")
    table.add_column("Taille", justify="right", style="bold")
    table.add_column("Nettoyable ?")

    total_cleanable = 0
    for name, size in caches:
        if name in safe_to_clean:
            status = "[green]✓ Oui (sans risque)[/green]"
            total_cleanable += size
        elif name in careful:
            status = "[yellow]~ Partiel (se recrée)[/yellow]"
        else:
            status = "[dim]? À vérifier[/dim]"
        table.add_row(name, fmt_size(size), status)

    console.print(table)
    console.print(f"\n[green]Nettoyable sans risque : {fmt_size(total_cleanable)}[/green]")


def clean_caches():
    console.print("\n[bold cyan]Nettoyage Caches[/bold cyan]\n")

    actions = []

    # pip
    pip_cache = HOME / ".cache" / "pip"
    if pip_cache.exists():
        size = dir_size(str(pip_cache))
        if size > 1024 * 1024:
            actions.append(("pip cache", size, "pip cache purge"))

    # playwright
    pw = HOME / ".cache" / "ms-playwright"
    if pw.exists():
        size = dir_size(str(pw))
        if size > 1024 * 1024:
            actions.append(("ms-playwright", size, f"rm -rf {pw}"))

    # electron
    el = HOME / ".cache" / "electron"
    if el.exists():
        size = dir_size(str(el))
        if size > 1024 * 1024:
            actions.append(("electron", size, f"rm -rf {el}"))

    # thumbnails
    th = HOME / ".cache" / "thumbnails"
    if th.exists():
        size = dir_size(str(th))
        if size > 1024 * 1024:
            actions.append(("thumbnails", size, f"rm -rf {th}"))

    # huggingface
    hf = HOME / ".cache" / "huggingface"
    if hf.exists():
        size = dir_size(str(hf))
        if size > 50 * 1024 * 1024:
            actions.append(("huggingface models", size, f"rm -rf {hf}"))

    if not actions:
        console.print("[green]Rien à nettoyer.[/green]")
        return

    for name, size, cmd in actions:
        if Confirm.ask(f"Nettoyer {name} ({fmt_size(size)}) ?"):
            run(cmd, timeout=60)
            console.print(f"  [green]✓ {name} nettoyé[/green]")


# ─── 8. Desktop ZIP duplicates ──────────────────────────────────────────────

def show_desktop_duplicates():
    desktop = HOME / "Desktop"
    if not desktop.exists():
        return

    console.print("\n[bold cyan]══ DOUBLONS DESKTOP ══[/bold cyan]\n")

    items = {}
    for item in desktop.iterdir():
        try:
            if item.is_file():
                items[item.name] = (item, item.stat().st_size)
            elif item.is_dir():
                items[item.name] = (item, dir_size(str(item)))
        except (PermissionError, OSError):
            pass

    duplicates = []
    for name, (path, size) in items.items():
        if path.suffix == ".zip":
            folder = name.replace(".zip", "")
            if folder in items:
                duplicates.append((path, size, f"Archive de '{folder}/'"))

    if duplicates:
        table = Table(box=box.SIMPLE, header_style="bold")
        table.add_column("Fichier ZIP", style="red")
        table.add_column("Taille", justify="right")
        table.add_column("Raison")
        total = 0
        for f, size, reason in duplicates:
            table.add_row(f.name, fmt_size(size), reason)
            total += size
        console.print(table)
        console.print(f"[bold red]Total : {fmt_size(total)}[/bold red]")
    else:
        console.print("[green]Aucun doublon détecté.[/green]")


# ─── 9. Suggestions ─────────────────────────────────────────────────────────

def show_suggestions():
    console.print("\n[bold cyan]══ SUGGESTIONS ══[/bold cyan]\n")

    suggestions = []

    # Trash
    trash_path = HOME / ".local" / "share" / "Trash"
    if trash_path.exists():
        size = dir_size(str(trash_path))
        if size > 100 * 1024 * 1024:
            suggestions.append(("CRITIQUE", f"Corbeille = {fmt_size(size)}", "pc-clean --clean trash"))

    # Docker
    if docker_available():
        stopped = run("docker ps -a --filter 'status=exited' -q")
        if stopped:
            count = len(stopped.splitlines())
            suggestions.append(("MOYEN", f"{count} containers Docker arrêtés", "pc-clean --clean docker"))

        dangling = run("docker images -f 'dangling=true' -q")
        if dangling:
            count = len(dangling.splitlines())
            suggestions.append(("MOYEN", f"{count} images Docker orphelines", "pc-clean --clean docker"))

        cache_info = run("docker system df --format '{{.Type}}\t{{.Size}}' | grep 'Build Cache'")
        if cache_info:
            suggestions.append(("MOYEN", f"Build cache Docker volumineux", "pc-clean --clean docker"))

    # pip cache
    pip_cache = HOME / ".cache" / "pip"
    if pip_cache.exists():
        size = dir_size(str(pip_cache))
        if size > 500 * 1024 * 1024:
            suggestions.append(("FAIBLE", f"Cache pip = {fmt_size(size)}", "pc-clean --clean caches"))

    # Downloads
    dl = HOME / "Downloads"
    if dl.exists():
        size = dir_size(str(dl))
        if size > 2 * 1024 * 1024 * 1024:
            suggestions.append(("MOYEN", f"Downloads = {fmt_size(size)}", "pc-clean --clean downloads"))

    # Gradle
    gradle = HOME / ".gradle"
    if gradle.exists():
        size = dir_size(str(gradle))
        if size > 500 * 1024 * 1024:
            suggestions.append(("FAIBLE", f"Cache Gradle = {fmt_size(size)}", "Nettoyable si pas de dev Android"))

    if not suggestions:
        console.print("[green]✓ Tout est propre ![/green]")
        return

    table = Table(box=box.ROUNDED, header_style="bold white")
    table.add_column("Priorité", justify="center")
    table.add_column("Problème", style="bold")
    table.add_column("Commande")

    colors = {"CRITIQUE": "bold red", "MOYEN": "yellow", "FAIBLE": "dim"}
    for prio, msg, cmd in suggestions:
        table.add_row(f"[{colors[prio]}]{prio}[/{colors[prio]}]", msg, f"[cyan]{cmd}[/cyan]")

    console.print(table)


# ─── 10. Full Dashboard ─────────────────────────────────────────────────────

def dashboard():
    console.print(Panel(
        "[bold white]pc-clean[/bold white] — Gestionnaire de stockage pour développeurs",
        style="cyan",
        box=box.DOUBLE,
    ))

    show_disk_overview()
    show_trash()
    show_home_breakdown()
    show_docker()
    show_ports()
    show_downloads()
    show_desktop_duplicates()
    show_caches()
    show_suggestions()


# ─── 11. Interactive Menu ────────────────────────────────────────────────────

def interactive():
    while True:
        console.print("\n[bold cyan]╔══ PC-CLEAN MENU ══╗[/bold cyan]")
        options = [
            ("1", "Dashboard complet"),
            ("2", "Disques & stockage"),
            ("3", "Docker"),
            ("4", "Ports en écoute"),
            ("5", "Downloads"),
            ("6", "Caches"),
            ("7", "Suggestions"),
            ("", "───── NETTOYAGE ─────"),
            ("c1", "Vider la corbeille"),
            ("c2", "Nettoyer Docker"),
            ("c3", "Nettoyer Downloads (doublons)"),
            ("c4", "Nettoyer Caches"),
            ("", "─────────────────────"),
            ("q", "Quitter"),
        ]

        for key, desc in options:
            if key:
                console.print(f"  [cyan][{key}][/cyan] {desc}")
            else:
                console.print(f"  [dim]{desc}[/dim]")

        choice = Prompt.ask("\nChoix", default="1")

        actions = {
            "1": dashboard,
            "2": lambda: (show_disk_overview(), show_home_breakdown()),
            "3": show_docker,
            "4": show_ports,
            "5": show_downloads,
            "6": show_caches,
            "7": show_suggestions,
            "c1": clean_trash,
            "c2": clean_docker,
            "c3": clean_downloads,
            "c4": clean_caches,
        }

        if choice == "q":
            console.print("[green]À bientôt ![/green]")
            break
        elif choice in actions:
            actions[choice]()
        else:
            console.print("[red]Choix invalide.[/red]")


# ─── CLI Entry ───────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="pc-clean — Gestionnaire de stockage pour développeurs")
    parser.add_argument("--dashboard", "-d", action="store_true", help="Afficher le dashboard complet")
    parser.add_argument("--docker", action="store_true", help="Analyser Docker")
    parser.add_argument("--ports", "-p", action="store_true", help="Scanner les ports")
    parser.add_argument("--downloads", action="store_true", help="Analyser Downloads")
    parser.add_argument("--caches", action="store_true", help="Analyser les caches")
    parser.add_argument("--suggestions", "-s", action="store_true", help="Afficher les suggestions")
    parser.add_argument("--clean", choices=["trash", "docker", "downloads", "caches", "all"],
                       help="Nettoyer une catégorie")
    parser.add_argument("--interactive", "-i", action="store_true", help="Mode interactif (défaut)")

    args = parser.parse_args()

    if args.clean:
        cleaners = {
            "trash": clean_trash,
            "docker": clean_docker,
            "downloads": clean_downloads,
            "caches": clean_caches,
            "all": lambda: (clean_trash(), clean_docker(), clean_downloads(), clean_caches()),
        }
        cleaners[args.clean]()
    elif args.dashboard:
        dashboard()
    elif args.docker:
        show_docker()
    elif args.ports:
        show_ports()
    elif args.downloads:
        show_downloads()
    elif args.caches:
        show_caches()
    elif args.suggestions:
        show_suggestions()
    else:
        interactive()


if __name__ == "__main__":
    main()
