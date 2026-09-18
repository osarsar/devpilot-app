"""Proniver Document Generator — Flask Blueprint for DevPilot."""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request, send_file

# Add proniver-docs to path for imports
PRONIVER_DIR = Path.home() / "proniver-docs"
sys.path.insert(0, str(PRONIVER_DIR))

from jinja2 import Environment, FileSystemLoader

docgen_bp = Blueprint("docgen", __name__)

TEMPLATES_DIR = PRONIVER_DIR / "templates"
CLIENTS_DIR = PRONIVER_DIR / "clients"
OUTPUT_DIR = PRONIVER_DIR / "output"

# Ensure dirs exist
CLIENTS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Jinja2 environment (same as generate.py)
# ---------------------------------------------------------------------------
env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=False)

def fmt_price(value):
    if value is None: return "—"
    try: return f"{int(value):,}".replace(",", " ")
    except (ValueError, TypeError): return str(value)

def fmt_date(value, fmt="%d/%m/%Y"):
    if not value: return "—"
    if isinstance(value, str):
        for p in ("%Y-%m-%d", "%d/%m/%Y"):
            try: value = datetime.strptime(value, p); break
            except ValueError: continue
        else: return value
    return value.strftime(fmt)

def fmt_date_long(value):
    mois = ["","janvier","fevrier","mars","avril","mai","juin",
            "juillet","aout","septembre","octobre","novembre","decembre"]
    if not value: return "—"
    if isinstance(value, str):
        try: value = datetime.strptime(value, "%Y-%m-%d")
        except ValueError: return value
    return f"{value.day} {mois[value.month]} {value.year}"

def add_days(value, days):
    if isinstance(value, str): value = datetime.strptime(value, "%Y-%m-%d")
    return (value + timedelta(days=days)).strftime("%Y-%m-%d")

env.filters["price"] = fmt_price
env.filters["date"] = fmt_date
env.filters["date_long"] = fmt_date_long
env.filters["add_days"] = add_days

# ---------------------------------------------------------------------------
# Pack definitions
# ---------------------------------------------------------------------------
PACKS = {
    "P1": {"nom":"P1 — CARTE","description":"Site 1 page","delai":"5 jours ouvres","creation":1900,"abonnement":149,
        "livrables":["Site 1 page (mobile d'abord) avec menu digital","QR code a imprimer","Horaires & plan Google Maps","Bouton WhatsApp & appel","Fiche Google Business creee et optimisee","Nom de domaine + hebergement + SSL"],
        "non_inclus":["Module de reservation ou commande en ligne","Galerie photo avancee","SEO au-dela de la fiche Google Business","Contenu redactionnel (facture 800 MAD si Proniver redige)","Shooting photo (facture 1 500 MAD)","Maintenance au-dela de 1 h/mois"]},
    "P2": {"nom":"P2 — VITRINE+","description":"Jusqu'a 5 pages + reservation","delai":"10 jours ouvres","creation":3900,"abonnement":249,
        "livrables":["Tout le pack P1","Jusqu'a 5 pages (Accueil, Menu, Galerie, A propos, Contact)","Module de reservation en ligne (notification WhatsApp + email)","Galerie photo professionnelle","SEO local sur 5 mots-cles","Bilingue FR / AR","Apercu soigne au partage WhatsApp (Open Graph)"],
        "non_inclus":["Commande en ligne / paiement","Back-office de gestion","Application mobile","Plus de 5 pages (400 MAD / page supplementaire)","Shooting photo (facture 1 500 MAD)","Contenu redactionnel (facture 800 MAD si Proniver redige)"]},
    "P3": {"nom":"P3 — COMMANDE","description":"Commande en ligne + back-office","delai":"20 jours ouvres","creation":7900,"abonnement":399,
        "livrables":["Tout le pack P2","Commande en ligne (panier, creneaux, zones de livraison)","Paiement en ligne CMI ou paiement a la livraison","Back-office de gestion des commandes et du menu","Notifications temps reel","Formation de l'equipe (2 h)"],
        "non_inclus":["Application mobile native","Connexion a un logiciel de caisse ou ERP","Espace membre / abonnements","Shooting photo (facture 1 500 MAD)"]},
    "P4": {"nom":"P4 — SUR MESURE","description":"Developpement specifique / app mobile","delai":"Au cahier des charges","creation":18000,"abonnement":700,
        "livrables":["Cahier des charges redige","Developpement specifique","Application mobile iOS / Android (si applicable)","Connexion a un logiciel de caisse ou ERP","Espace membre / abonnements","Maintenance evolutive"],
        "non_inclus":["Defini au cas par cas dans le cahier des charges"]},
}

ABONNEMENT_INCLUS = [
    "Hebergement performant","Nom de domaine renouvele","Certificat SSL",
    "Sauvegardes automatiques quotidiennes","Surveillance de la disponibilite",
    "Mises a jour de securite","1 heure de modifications par mois",
    "Support WhatsApp 6 j / 7","Rapport de frequentation mensuel (a partir de P2)",
]

DOC_TYPES = {
    "devis": {"label": "Devis", "template": "devis.html", "icon": "file-text"},
    "facture-acompte": {"label": "Facture Acompte", "template": "facture.html", "icon": "receipt"},
    "facture-solde": {"label": "Facture Solde", "template": "facture.html", "icon": "receipt"},
    "fiche": {"label": "Fiche Qualification", "template": "fiche_qualification.html", "icon": "clipboard"},
    "contenu": {"label": "Fiche Contenu", "template": "fiche_contenu.html", "icon": "list"},
    "golive": {"label": "Checklist Go-Live", "template": "checklist_golive.html", "icon": "check-circle"},
    "avenant": {"label": "Avenant", "template": "avenant.html", "icon": "file-plus"},
    "rapport": {"label": "Rapport Mensuel", "template": "rapport_mensuel.html", "icon": "bar-chart"},
    "guide": {"label": "Guide Utilisation", "template": "guide_utilisation.html", "icon": "book"},
    "messages": {"label": "Scripts Messages", "template": "messages.html", "icon": "message-circle"},
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_client(ref):
    path = CLIENTS_DIR / f"{ref}.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data

def _enrich_client(data):
    """Add computed fields for template rendering."""
    pack_key = data.get("pack", "P2")
    data["pack_details"] = PACKS.get(pack_key, PACKS["P2"])
    data["abonnement_inclus"] = ABONNEMENT_INCLUS
    pack_order = ["P1", "P2", "P3", "P4"]
    idx = pack_order.index(pack_key) if pack_key in pack_order else 1
    data["pack_option2"] = PACKS[pack_order[idx + 1]] if idx < len(pack_order) - 1 else None
    data["today"] = datetime.now().strftime("%Y-%m-%d")
    data["today_long"] = fmt_date_long(data["today"])
    sig = data.get("date_signature") or data["today"]
    data["date_validite"] = add_days(sig, 15)
    return data

def _render_doc(template_name, data):
    tpl = env.get_template(template_name)
    return tpl.render(**data)

def _generate_pdf(html_str, output_path):
    try:
        from weasyprint import HTML
        HTML(string=html_str, base_url=str(TEMPLATES_DIR)).write_pdf(str(output_path))
        return True
    except Exception as e:
        print(f"PDF generation error: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════════════════

@docgen_bp.route("/api/proniver/clients")
def api_proniver_clients():
    """List all clients."""
    clients = []
    for f in sorted(CLIENTS_DIR.glob("PRN-*.json")):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            clients.append({
                "reference": d.get("reference", f.stem),
                "nom": d.get("etablissement", {}).get("nom", ""),
                "quartier": d.get("etablissement", {}).get("quartier", ""),
                "scoring": d.get("scoring", ""),
                "pack": d.get("pack", ""),
                "statut": d.get("statut", ""),
                "date_premier_contact": d.get("date_premier_contact", ""),
            })
        except Exception:
            continue
    return jsonify(clients)


@docgen_bp.route("/api/proniver/clients", methods=["POST"])
def api_proniver_create_client():
    """Create a new client."""
    data = request.json or {}
    ref = data.get("reference", "")
    nom = data.get("nom", "")
    if not ref or not nom:
        return jsonify({"success": False, "message": "Reference et nom requis"})

    client = {
        "reference": ref,
        "etablissement": {
            "nom": nom,
            "quartier": data.get("quartier", ""),
            "adresse": "",
            "telephone": data.get("telephone", ""),
            "instagram": data.get("instagram", ""),
            "google_avis": data.get("google_avis", 0),
            "google_note": data.get("google_note", 0.0),
            "site_existant": False,
            "menu_accessible": False,
            "reservation_possible": False,
        },
        "decideur": {
            "nom": data.get("decideur_nom", ""),
            "prenom": data.get("decideur_prenom", ""),
            "role": data.get("decideur_role", "Proprietaire"),
            "telephone": data.get("telephone", ""),
            "email": data.get("decideur_email", ""),
        },
        "scoring": data.get("scoring", "B"),
        "pack": data.get("pack", "P2"),
        "creation_prix": None,
        "abonnement_prix": None,
        "domaine": "",
        "frais_domaine": 150,
        "frais_domaine_nom": ".ma",
        "frais_supplementaires": [],
        "remise_percent": 0,
        "remise_raison": "",
        "statut": "prospect",
        "date_premier_contact": datetime.now().strftime("%Y-%m-%d"),
        "date_signature": None,
        "date_j0": None,
        "delai_personnalise": None,
        "probleme_client": data.get("probleme_client", ""),
        "avenant": {"description": "", "montant": 0, "raison": ""},
        "rapport": {"mois": "", "visiteurs": 0, "clics_whatsapp": 0, "reservations": 0,
                     "positions_google": [], "actions_realisees": [], "recommandations": []},
        "notes": "",
    }
    path = CLIENTS_DIR / f"{ref}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(client, f, ensure_ascii=False, indent=2)
    return jsonify({"success": True, "message": f"Client {ref} cree", "reference": ref})


@docgen_bp.route("/api/proniver/clients/<ref>")
def api_proniver_get_client(ref):
    """Get client data."""
    data = _load_client(ref)
    if not data:
        return jsonify({"success": False, "message": "Client introuvable"}), 404
    return jsonify(data)


@docgen_bp.route("/api/proniver/clients/<ref>", methods=["PUT"])
def api_proniver_update_client(ref):
    """Update client data."""
    path = CLIENTS_DIR / f"{ref}.json"
    if not path.exists():
        return jsonify({"success": False, "message": "Client introuvable"}), 404
    updates = request.json or {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Deep merge
    for key, val in updates.items():
        if isinstance(val, dict) and isinstance(data.get(key), dict):
            data[key].update(val)
        else:
            data[key] = val
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return jsonify({"success": True, "message": "Client mis a jour"})


@docgen_bp.route("/api/proniver/generate", methods=["POST"])
def api_proniver_generate():
    """Generate one or more documents."""
    req = request.json or {}
    ref = req.get("reference", "")
    doc_type = req.get("doc_type", "")  # e.g. "devis", "facture-acompte", "all"

    data = _load_client(ref)
    if not data:
        return jsonify({"success": False, "message": "Client introuvable"})

    data = _enrich_client(data)
    generated = []

    types_to_gen = list(DOC_TYPES.keys()) if doc_type == "all" else [doc_type]

    for dtype in types_to_gen:
        if dtype not in DOC_TYPES:
            continue
        info = DOC_TYPES[dtype]
        tpl_name = info["template"]

        # Special handling for factures
        if dtype == "facture-acompte":
            data["type_facture"] = "acompte"
            data["facture_numero"] = f"FA-{ref}-A"
            pack = data["pack_details"]
            prix = data.get("creation_prix") or pack["creation"]
            remise = data.get("remise_percent", 0)
            if remise: prix = int(prix * (1 - remise / 100))
            data["facture_montant"] = int(prix * 0.5)
            data["facture_label"] = "Acompte 50 % — creation de site"
            data["facture_total"] = data["facture_montant"]
            data["frais_lines"] = []
        elif dtype == "facture-solde":
            data["type_facture"] = "solde"
            data["facture_numero"] = f"FA-{ref}-S"
            pack = data["pack_details"]
            prix = data.get("creation_prix") or pack["creation"]
            remise = data.get("remise_percent", 0)
            if remise: prix = int(prix * (1 - remise / 100))
            data["facture_montant"] = int(prix * 0.5)
            data["facture_label"] = "Solde 50 % — livraison et mise en ligne"
            data["facture_total"] = data["facture_montant"]
            data["frais_lines"] = []
            if data.get("frais_domaine"):
                data["frais_lines"].append({"label": f"Nom de domaine {data.get('frais_domaine_nom','.ma')} (refacture)", "montant": data["frais_domaine"]})
                data["facture_total"] += data["frais_domaine"]

        try:
            html = _render_doc(tpl_name, data)
            out_dir = OUTPUT_DIR / ref
            out_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{datetime.now():%Y%m%d}_{ref}_{dtype}"

            # Save HTML
            html_path = out_dir / f"{filename}.html"
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html)

            # Generate PDF
            pdf_path = out_dir / f"{filename}.pdf"
            pdf_ok = _generate_pdf(html, pdf_path)

            generated.append({
                "type": dtype,
                "label": info["label"],
                "filename": f"{filename}.pdf" if pdf_ok else f"{filename}.html",
                "pdf": pdf_ok,
            })
        except Exception as e:
            generated.append({"type": dtype, "label": info["label"], "error": str(e)})

    return jsonify({
        "success": True,
        "message": f"{len(generated)} document(s) genere(s)",
        "documents": generated,
    })


@docgen_bp.route("/api/proniver/documents/<ref>")
def api_proniver_documents(ref):
    """List generated documents for a client."""
    out_dir = OUTPUT_DIR / ref
    if not out_dir.exists():
        return jsonify([])
    files = []
    for f in sorted(out_dir.glob("*.pdf"), reverse=True):
        files.append({
            "filename": f.name,
            "size": f.stat().st_size,
            "size_h": f"{f.stat().st_size / 1024:.0f} KB",
            "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return jsonify(files)


@docgen_bp.route("/api/proniver/download/<ref>/<filename>")
def api_proniver_download(ref, filename):
    """Download a generated document."""
    path = OUTPUT_DIR / ref / filename
    if not path.exists():
        return jsonify({"success": False, "message": "Fichier introuvable"}), 404
    return send_file(str(path), as_attachment=True, download_name=filename)


@docgen_bp.route("/api/proniver/packs")
def api_proniver_packs():
    """Return pack definitions."""
    return jsonify(PACKS)


# ═══════════════════════════════════════════════════════════════════════════
# COMPANY DOCUMENTS
# ═══════════════════════════════════════════════════════════════════════════

COMPANY_DOCS = {
    "carte_visite": {"label": "Carte de visite", "template": "carte_visite.html", "icon": "credit-card", "desc": "90 x 60 mm — recto/verso, reperes de decoupe"},
    "papier_entete": {"label": "Papier en-tete", "template": "papier_entete.html", "icon": "file-text", "desc": "A4 — courrier formel avec logo et bande navy"},
    "presentation": {"label": "Presentation commerciale", "template": "presentation.html", "icon": "briefcase", "desc": "One-pager — 3 promesses, 4 packs, CTA"},
    "signature_email": {"label": "Signature email", "template": "signature_email.html", "icon": "at-sign", "desc": "HTML — apercu + code a copier dans Gmail"},
}


@docgen_bp.route("/api/proniver/entreprise/generate", methods=["POST"])
def api_proniver_entreprise_generate():
    """Generate a company document."""
    req = request.json or {}
    doc_type = req.get("doc_type", "")

    if doc_type == "all":
        types_to_gen = list(COMPANY_DOCS.keys())
    elif doc_type in COMPANY_DOCS:
        types_to_gen = [doc_type]
    else:
        return jsonify({"success": False, "message": f"Type inconnu: {doc_type}"})

    generated = []
    for dtype in types_to_gen:
        info = COMPANY_DOCS[dtype]
        try:
            out_dir = OUTPUT_DIR / "entreprise"
            out_dir.mkdir(parents=True, exist_ok=True)

            # Carte de visite: use the pre-designed PDF
            if dtype == "carte_visite":
                import shutil
                src = PRONIVER_DIR / "static" / "carte_visite_original.pdf"
                dst = out_dir / "carte_visite.pdf"
                if src.exists():
                    shutil.copy2(str(src), str(dst))
                    generated.append({"type": dtype, "label": info["label"], "filename": "carte_visite.pdf", "pdf": True})
                    continue

            tpl = env.get_template(info["template"])
            html = tpl.render()

            html_path = out_dir / f"{dtype}.html"
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html)

            pdf_path = out_dir / f"{dtype}.pdf"
            pdf_ok = _generate_pdf(html, pdf_path)

            generated.append({
                "type": dtype,
                "label": info["label"],
                "filename": f"{dtype}.pdf" if pdf_ok else f"{dtype}.html",
                "pdf": pdf_ok,
            })
        except Exception as e:
            generated.append({"type": dtype, "label": info["label"], "error": str(e)})

    return jsonify({
        "success": True,
        "message": f"{len(generated)} document(s) genere(s)",
        "documents": generated,
    })


@docgen_bp.route("/api/proniver/entreprise/documents")
def api_proniver_entreprise_documents():
    """List generated company documents."""
    out_dir = OUTPUT_DIR / "entreprise"
    if not out_dir.exists():
        return jsonify([])
    files = []
    for f in sorted(out_dir.glob("*.pdf"), reverse=True):
        doc_key = f.stem
        meta = COMPANY_DOCS.get(doc_key, {})
        files.append({
            "filename": f.name,
            "key": doc_key,
            "label": meta.get("label", f.stem),
            "desc": meta.get("desc", ""),
            "size": f.stat().st_size,
            "size_h": f"{f.stat().st_size / 1024:.0f} KB",
            "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return jsonify(files)


@docgen_bp.route("/api/proniver/entreprise/download/<filename>")
def api_proniver_entreprise_download(filename):
    """Download a company document."""
    path = OUTPUT_DIR / "entreprise" / filename
    if not path.exists():
        return jsonify({"success": False, "message": "Fichier introuvable"}), 404
    return send_file(str(path), as_attachment=False, download_name=filename)


@docgen_bp.route("/api/proniver/entreprise/preview/<filename>")
def api_proniver_entreprise_preview(filename):
    """Preview a company document (serve inline)."""
    # For PDF files that are pre-designed (carte_visite), serve PDF directly
    pdf_path = OUTPUT_DIR / "entreprise" / filename
    if filename.endswith(".pdf") and pdf_path.exists():
        # Check if there's a corresponding HTML version (for HTML-generated docs)
        html_name = filename.replace(".pdf", ".html")
        html_path = OUTPUT_DIR / "entreprise" / html_name
        # If HTML exists AND it's not the carte_visite (which is a pre-designed PDF), use HTML
        if html_path.exists() and "carte_visite" not in filename:
            return send_file(str(html_path), mimetype="text/html")
        # Otherwise serve the PDF inline
        return send_file(str(pdf_path), mimetype="application/pdf")
    # For explicit HTML requests
    path = OUTPUT_DIR / "entreprise" / filename
    if not path.exists():
        return jsonify({"success": False, "message": "Fichier introuvable"}), 404
    return send_file(str(path), mimetype="text/html" if path.suffix == ".html" else "application/pdf")


# ═══════════════════════════════════════════════════════════════════════════
# MAQUETTE-APPAT (P3)
# ═══════════════════════════════════════════════════════════════════════════

MAQUETTES_DIR = PRONIVER_DIR / "output" / "maquettes"
UPLOADS_DIR = PRONIVER_DIR / "uploads"

@docgen_bp.route("/api/proniver/maquette/upload", methods=["POST"])
def api_maquette_upload():
    """Upload a photo for a maquette."""
    if "photo" not in request.files:
        return jsonify({"success": False, "message": "Aucun fichier"})
    f = request.files["photo"]
    slug = request.form.get("slug", "temp")
    upload_dir = UPLOADS_DIR / slug
    upload_dir.mkdir(parents=True, exist_ok=True)
    # Save with timestamp to avoid collisions
    fname = f"{datetime.now():%H%M%S}_{f.filename}"
    fpath = upload_dir / fname
    f.save(str(fpath))
    return jsonify({
        "success": True,
        "filename": fname,
        "url": f"/api/proniver/maquette/photo/{slug}/{fname}",
    })


@docgen_bp.route("/api/proniver/maquette/photo/<slug>/<filename>")
def api_maquette_photo(slug, filename):
    """Serve an uploaded photo."""
    path = UPLOADS_DIR / slug / filename
    if not path.exists():
        return jsonify({"success": False}), 404
    return send_file(str(path))


@docgen_bp.route("/api/proniver/maquette/generate", methods=["POST"])
def api_maquette_generate():
    """Generate a maquette from client-rendered HTML."""
    import re as _re
    data = request.json or {}
    slug = data.get("slug", "")
    html = data.get("html", "")
    if not slug or not html:
        return jsonify({"success": False, "message": "Slug et HTML requis"})

    try:
        out_dir = MAQUETTES_DIR / slug
        out_dir.mkdir(parents=True, exist_ok=True)

        # Save HTML
        (out_dir / "index.html").write_text(html, encoding="utf-8")

        # For PDF: replace server photo URLs with local file paths so WeasyPrint can resolve them
        html_for_pdf = _re.sub(
            r'/api/proniver/maquette/photo/([^/]+)/([^"\'>\s]+)',
            lambda m: "file://" + str(UPLOADS_DIR / m.group(1) / m.group(2)),
            html,
        )
        pdf_path = out_dir / "maquette.pdf"
        pdf_ok = _generate_pdf(html_for_pdf, pdf_path)

        # Save metadata
        meta = {k: data.get(k, "") for k in ("nom", "quartier", "type_activite")}
        (out_dir / "data.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        return jsonify({
            "success": True,
            "message": f"Maquette generee",
            "slug": slug,
            "html_url": f"/api/proniver/maquette/preview/{slug}",
            "pdf_url": f"/api/proniver/maquette/download/{slug}",
            "pdf": pdf_ok,
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@docgen_bp.route("/api/proniver/maquette/preview/<slug>")
def api_maquette_preview(slug):
    """Preview a maquette (serve HTML)."""
    path = MAQUETTES_DIR / slug / "index.html"
    if not path.exists():
        return jsonify({"success": False, "message": "Maquette introuvable"}), 404
    return send_file(str(path), mimetype="text/html")


@docgen_bp.route("/api/proniver/maquette/download/<slug>")
def api_maquette_download(slug):
    """Download maquette PDF."""
    path = MAQUETTES_DIR / slug / "maquette.pdf"
    if not path.exists():
        return jsonify({"success": False, "message": "PDF introuvable"}), 404
    return send_file(str(path), as_attachment=True, download_name=f"apercu-{slug}.pdf")


@docgen_bp.route("/api/proniver/maquettes")
def api_maquette_list():
    """List all generated maquettes."""
    if not MAQUETTES_DIR.exists():
        return jsonify([])
    maquettes = []
    for d in sorted(MAQUETTES_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        data_path = d / "data.json"
        info = {"slug": d.name, "nom": d.name}
        if data_path.exists():
            import json as _json
            with open(data_path, "r", encoding="utf-8") as f:
                saved = _json.load(f)
            info["nom"] = saved.get("nom", d.name)
            info["quartier"] = saved.get("quartier", "")
            info["type_activite"] = saved.get("type_activite", "")
        info["has_pdf"] = (d / "maquette.pdf").exists()
        info["has_html"] = (d / "index.html").exists()
        maquettes.append(info)
    return jsonify(maquettes)
