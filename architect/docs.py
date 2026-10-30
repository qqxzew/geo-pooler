"""German draft documents for a pool: Flächenpoolvertrag (Entwurf) and Netzanschlussbegehren (PDF).

Both are TEMPLATES generated from the computed plan. They are drafts for review by a German lawyer — the
contract in particular touches Gesellschaftsrecht, Grundstücksrecht (§ 550/§ 311b BGB) and tax. Only WinAnsi
characters are used so the standard PDF fonts render ä ö ü ß § „ “ – € correctly.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from escrow.nda import ST_BODY, ST_H, ST_ITEM, ST_PARTY, ST_SMALL, ST_SUB, ST_TITLE, Party, _NumberedCanvas

POOL_TEMPLATE = "POOL-DE-1.0"
GRID_TEMPLATE = "NAB-DE-1.0"
PLACEHOLDER = "[bitte ergänzen]"


@dataclass
class DocParams:
    pool_id: str
    match_id: str
    created_at: datetime
    platform_name: str = "Flächenpool-Plattform"
    nda_sha256: str = ""
    nda_id: str = ""
    jurisdiction_city: str = "Berlin"
    exclusivity_months: int = 24
    term_years: int = 30


def _de(n: float, nd: int = 0) -> str:
    s = f"{n:,.{nd}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _pct(bp: int) -> str:
    return f"{_de(bp / 100, 2)} %"


def _eur(n: float) -> str:
    return f"{_de(n)} EUR"


def _party(p: dict[str, Any]) -> Party:
    c = p["company"]
    return Party(company_name=c["company_name"], street=c["street"], postal_code=c["postal_code"], city=c["city"],
                 representative=c["representative"], register=c.get("commercial_register", ""), email=c.get("email", ""))


def _tech(t: str) -> str:
    return {"wind": "Windenergie", "solar": "Photovoltaik"}.get(t, t)


def _table(rows: list[list[str]], widths: list[float], header: bool = True) -> Table:
    t = Table([[Paragraph(escape(str(c)).replace("\n", "<br/>"), ST_SMALL) for c in r] for r in rows], colWidths=[w * cm for w in widths], repeatRows=1 if header else 0)
    style = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#aaaaaa")), ("VALIGN", (0, 0), (-1, -1), "TOP")]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")))
    t.setStyle(TableStyle(style))
    return t


def _build(story: list, title: str, template: str, ident: str, params: DocParams, footer_note: str) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=2.2 * cm, rightMargin=2.2 * cm, topMargin=2.0 * cm, bottomMargin=2.2 * cm,
        title=title, author=params.platform_name, subject=f"{title} (Vermittlung {params.match_id})",
        creator=f"{params.platform_name} · {template}", keywords=template, lang="de-DE", invariant=1)

    class Canvas(_NumberedCanvas):
        footer_text = f"{template} · Kennung {ident} · {footer_note}"

    doc.build(story, canvasmaker=lambda *a, **k: Canvas(*a, **k))
    return buf.getvalue()


def _paras(items: list[tuple[str, list[str]]]) -> list:
    out: list = []
    for heading, paras in items:
        out.append(Paragraph(escape(heading), ST_H))
        for t in paras:
            out.append(Paragraph(escape(t.strip()), ST_ITEM if t.startswith("  ") else ST_BODY))
    return out


# ================================================================== Flächenpoolvertrag


def _pool_sections(plan: dict[str, Any], pa: Party, pb: Party, params: DocParams) -> list[tuple[str, list[str]]]:
    sa, sb = plan["parties"][0], plan["parties"][1]
    bp = plan["shares_bp"]
    method = {"shapley": "dem Shapley-Wert (jede Partei behält den Wert ihrer Einzelentwicklung, der Kooperationsgewinn wird hälftig geteilt)",
              "area": "dem Verhältnis der eingebrachten Flächen", "revenue": "dem Verhältnis der den jeweiligen Flächen zuzurechnenden Jahreserlöse"}[plan["method_applied"]]
    jp = plan["joint_project"]
    return [
        ("§ 1 Gegenstand und Zweck", [
            "(1) Die Parteien beabsichtigen, auf ihren in Anlage 1 bezeichneten, räumlich benachbarten Flächen gemeinsam ein "
            "Vorhaben zur Erzeugung erneuerbarer Energien zu entwickeln, zu errichten und zu betreiben (das „Projekt“) und hierzu "
            "ihre Flächen in einem Flächenpool zusammenzuführen.",
            f"(2) Das Projekt umfasst nach dem Planungsstand der Anlage 2 voraussichtlich eine installierte Leistung von rund "
            f"{_de(jp['capacity_mw'], 1)} MW. Die Anlage 2 ist eine überschlägige Vorplanung, keine Genehmigungs- oder Ausführungsplanung.",
            "(3) Dieser Vertrag regelt die Zusammenarbeit bis zur Gründung der Projektgesellschaft und deren Grundzüge. "
            "Soweit Regelungen eine notarielle Beurkundung erfordern, werden sie in den Gesellschaftsvertrag bzw. in gesonderte "
            "Urkunden übernommen.",
        ]),
        ("§ 2 Projektgesellschaft", [
            "(1) Die Parteien gründen für das Projekt eine Projektgesellschaft in der Rechtsform einer GmbH & Co. KG [Rechtsform "
            "abzustimmen] (die „Projektgesellschaft“) mit Sitz in [Sitz ergänzen]. Bis zu deren Eintragung bilden die Parteien eine "
            "Gesellschaft bürgerlichen Rechts, deren Zweck auf die Vorbereitung des Projekts beschränkt ist.",
            "(2) Die Parteien sind an der Projektgesellschaft nach Maßgabe von § 3 beteiligt. Die Beteiligungsquoten bestimmen sich "
            "auch für Stimmrechte, Kapitalanteile, Gewinn- und Verlustverteilung und Liquidationserlös, soweit der Gesellschaftsvertrag "
            "nichts Abweichendes bestimmt.",
        ]),
        ("§ 3 Beteiligungsquoten", [
            f"(1) Die Beteiligung beträgt: Partei A {_pct(bp['A'])}, Partei B {_pct(bp['B'])}.",
            f"(2) Die Quoten wurden nach {method} auf Grundlage der Berechnung in Anlage 2 ermittelt. Die Berechnung beruht auf "
            "Standardannahmen und Daten öffentlicher Quellen; sie ist unverbindlich und von den Parteien zu überprüfen.",
            "(3) Ergeben sich vor Erteilung der Genehmigung wesentliche Änderungen (insbesondere Anlagenzahl oder -leistung, "
            "Standorte der Anlagen, Wegfall oder Einschränkung einer Fläche, geänderte Netzanschlusskosten), verhandeln die Parteien "
            "die Quoten nach Treu und Glauben neu. Kommt keine Einigung zustande, entscheidet auf Antrag einer Partei ein von der "
            "IHK am Sitz der Projektgesellschaft zu benennender Sachverständiger als Schiedsgutachter (§ 317 BGB).",
        ]),
        ("§ 4 Flächen; Flächensicherung", [
            "(1) Jede Partei verpflichtet sich, die in Anlage 1 bezeichneten Flächen nach Maßgabe der Projektplanung für das Projekt "
            "bereitzustellen und der Projektgesellschaft die erforderlichen Nutzungsrechte einzuräumen bzw. durch ihre Eigentümer "
            "einräumen zu lassen (insbesondere Nutzungs-, Pacht-, Gestattungs- und Dienstbarkeitsverträge). Die Verträge sind schriftlich "
            "abzuschließen (§ 550 BGB) und, soweit erforderlich, dinglich zu sichern.",
            "(2) Jede Partei versichert, dass sie über die in Anlage 1 bezeichneten Flächen vertragliche Nutzungs- oder Optionsrechte "
            "besitzt, dass diesen Rechten keine entgegenstehenden Rechte Dritter bekannt sind und dass sie ihre Eigentümer und "
            "Verpächter über die Übertragung der Rechte auf die Projektgesellschaft informieren wird, soweit eine Zustimmung erforderlich ist.",
            "(3) Die Beziehungen jeder Partei zu den Eigentümern ihrer Flächen werden allein von dieser Partei geführt, bis die "
            "Projektgesellschaft in die Verträge eingetreten ist. § 4 der Geheimhaltungs- und Nichtumgehungsvereinbarung bleibt unberührt.",
        ]),
        ("§ 5 Kosten und Finanzierung", [
            "(1) Die Entwicklungskosten bis zur Genehmigung (Gutachten, Genehmigungsverfahren, Rechtsberatung, Netzanschlussuntersuchung, "
            f"Flächensicherung) werden von den Parteien im Verhältnis der Beteiligungsquoten nach § 3 getragen; die Entwicklungskosten "
            f"wurden in Anlage 2 überschlägig mit {_eur(jp['development_cost_eur'])} angesetzt.",
            "(2) Das Eigenkapital der Projektgesellschaft bringen die Parteien im Verhältnis ihrer Quoten auf. Die Parteien streben "
            "eine Projektfinanzierung durch Dritte an; Sicherheiten werden nur mit Zustimmung der betroffenen Partei gestellt.",
            "(3) Vorleistungen einer Partei, die über ihre Quote hinausgehen, sind der Projektgesellschaft darlehensweise zu "
            "marktüblichen Konditionen zu überlassen oder anderweitig auszugleichen.",
        ]),
        ("§ 6 Netzanschluss", [
            "(1) Die Parteien beantragen einen gemeinsamen Netzanschluss. Das Netzanschlussbegehren (Anlage 3) wird von der "
            "Projektgesellschaft in Gründung bzw. den Parteien gemeinsam gestellt; Anschlusskosten und Baukostenzuschüsse trägt die "
            "Projektgesellschaft.",
            f"(2) Für die gemeinsame Anbindung wurden Netzanschlusskosten von rund {_eur(jp['grid_connection_cost_eur'])} angesetzt "
            f"(einschließlich rund {_de(jp['internal_cable_m'])} m interner Verbindung der Flächen). Dies ist eine Schätzung; verbindlich "
            "sind allein die Angaben des Netzbetreibers.",
        ]),
        ("§ 7 Geschäftsführung und Beschlüsse", [
            "(1) Die Geschäftsführung der Projektgesellschaft wird von [Partei/Person bestimmen] wahrgenommen. Gesellschafterbeschlüsse "
            "werden mit einfacher Mehrheit der Stimmen nach Quoten gefasst, soweit nichts anderes bestimmt ist.",
            "(2) Einstimmigkeit ist erforderlich für: Änderung des Gesellschaftsvertrags, Aufnahme neuer Gesellschafter, Veräußerung "
            "des Projekts oder wesentlicher Teile, Kapitalmaßnahmen, Aufnahme von Fremdkapital über [Betrag] EUR, Abschluss von Verträgen "
            "mit nahestehenden Personen sowie die Änderung der Anlagenzahl oder -standorte.",
        ]),
        ("§ 8 Erlöse und Ergebnisverteilung", [
            "Erlöse aus dem Betrieb werden nach Abzug der Betriebskosten, des Kapitaldienstes und angemessener Rücklagen im "
            "Verhältnis der Beteiligungsquoten an die Gesellschafter ausgeschüttet. Pachtzahlungen an Eigentümer werden von der "
            "Projektgesellschaft als Betriebskosten getragen.",
        ]),
        ("§ 9 Exklusivität und Wettbewerb", [
            f"(1) Für die Dauer von {params.exclusivity_months} Monaten ab Unterzeichnung entwickeln die Parteien auf den in Anlage 1 "
            "bezeichneten Flächen kein Konkurrenzprojekt und führen keine Verhandlungen mit Dritten über deren Nutzung für erneuerbare Energien.",
            "(2) Erlischt das Projekt endgültig (insbesondere Versagung der Genehmigung ohne Aussicht auf Erfolg, endgültige "
            "Netzanschlussverweigerung), endet die Exklusivität sechs Monate danach.",
        ]),
        ("§ 10 Vertraulichkeit", [
            f"Die zwischen den Parteien geschlossene Geheimhaltungs- und Nichtumgehungsvereinbarung"
            + (f" (Kennung {params.nda_id}, Prüfwert SHA-256 {params.nda_sha256})" if params.nda_sha256 else "")
            + " gilt für diesen Vertrag und das Projekt fort; der Inhalt dieses Vertrags ist Vertrauliche Information im Sinne jener Vereinbarung.",
        ]),
        ("§ 11 Veräußerung; Vorkaufsrecht; Ausstieg", [
            "(1) Gesellschaftsanteile dürfen nur mit Zustimmung der anderen Partei veräußert werden. Der anderen Partei steht ein "
            "Vorkaufsrecht zu den Bedingungen des Dritten zu.",
            "(2) Scheitert das Projekt, trägt jede Partei die auf sie entfallenden Kosten nach § 5. Die Flächen fallen unbelastet "
            "an die jeweils einbringende Partei zurück.",
            "(3) Ein Recht zur außerordentlichen Kündigung aus wichtigem Grund (§ 314 BGB bzw. § 723 BGB) bleibt unberührt.",
        ]),
        ("§ 12 Laufzeit", [
            f"Dieser Vertrag gilt bis zur Gründung der Projektgesellschaft und deren Eintritt in die Flächenverträge, längstens "
            f"für {params.term_years} Jahre ab Unterzeichnung; die Regelungen des Gesellschaftsvertrags gehen danach vor.",
        ]),
        ("§ 13 Schlussbestimmungen", [
            "(1) Änderungen und Ergänzungen dieses Vertrags, auch dieser Klausel, bedürfen der Schriftform, soweit nicht gesetzlich "
            "eine strengere Form vorgeschrieben ist. Die Anlagen sind Vertragsbestandteil.",
            "(2) Sollte eine Bestimmung unwirksam oder undurchführbar sein, bleibt die Wirksamkeit im Übrigen unberührt; an die Stelle "
            "der unwirksamen Bestimmung tritt die wirksame Regelung, die dem wirtschaftlichen Zweck am nächsten kommt.",
            "(3) Es gilt deutsches Recht unter Ausschluss des UN-Kaufrechts. Ausschließlicher Gerichtsstand ist, soweit gesetzlich "
            f"zulässig, {params.jurisdiction_city}.",
        ]),
    ]


def _plan_tables(plan: dict[str, Any], package: dict[str, Any]) -> list:
    jp = plan["joint_project"]
    rows1 = [["", "Partei A", "Partei B"]]
    pa, pb = plan["parties"]
    fa, fb = package["parties"][0]["parcel"], package["parties"][1]["parcel"]
    rows1 += [
        ["Unternehmen", pa["company_name"], pb["company_name"]],
        ["Fläche (ha)", _de(fa["area_ha"], 2), _de(fb["area_ha"], 2)],
        ["Gemeinde / Land", f"{fa['municipality'] or '-'} / {fa['bundesland'] or '-'}", f"{fb['municipality'] or '-'} / {fb['bundesland'] or '-'}"],
        ["Flurstücke (ALKIS)", fa.get("alkis_id") or "-", fb.get("alkis_id") or "-"],
        ["Technologie", _tech(pa["parcel"]["technology"]), _tech(pb["parcel"]["technology"])],
        ["Audit (Verdikt / Score)", f"{fa['verdict']} / {fa['score']}", f"{fb['verdict']} / {fb['score']}"],
    ]
    rows2 = [["", "Partei A", "Partei B"]]
    rows2 += [
        ["Leistung im Projekt (MW)", _de(pa["in_joint_project"]["capacity_mw"], 2), _de(pb["in_joint_project"]["capacity_mw"], 2)],
        ["Jahresenergie (MWh/a)", _de(pa["in_joint_project"]["energy_mwh_a"]), _de(pb["in_joint_project"]["energy_mwh_a"])],
        ["Jahreserlös (EUR/a)", _de(pa["in_joint_project"]["revenue_eur_a"]), _de(pb["in_joint_project"]["revenue_eur_a"])],
        ["Einzelentwicklung: Kapitalwert (EUR)", _de(pa["standalone"]["npv_eur"]), _de(pb["standalone"]["npv_eur"])],
        ["Wertbeitrag (Shapley, EUR)", _de(pa["value_contribution_shapley_eur"]), _de(pb["value_contribution_shapley_eur"])],
        ["Quote nach Fläche", _pct(pa["share_bp"]["area"]), _pct(pb["share_bp"]["area"])],
        ["Quote nach Erlös", _pct(pa["share_bp"]["revenue"]), _pct(pb["share_bp"]["revenue"])],
        ["Quote nach Shapley", _pct(pa["share_bp"]["shapley"]), _pct(pb["share_bp"]["shapley"])],
        ["Vereinbarte Quote", _pct(pa["share_bp"]["chosen"]), _pct(pb["share_bp"]["chosen"])],
    ]
    syn = plan["synergy"]
    rows3 = [["Gemeinsames Projekt", "Wert"],
             ["Leistung (MW)", _de(jp["capacity_mw"], 2)], ["Jahresenergie (MWh/a)", _de(jp["energy_mwh_a"])],
             ["Jahreserlös (EUR/a)", _de(jp["revenue_eur_a"])], ["Investition Anlagen (EUR)", _de(jp["capex_eur"])],
             ["Netzanschluss (EUR)", _de(jp["grid_connection_cost_eur"])], ["Entwicklungskosten (EUR)", _de(jp["development_cost_eur"])],
             ["Kapitalwert gemeinsam (EUR)", _de(jp["npv_eur"])],
             ["Kooperationsgewinn (EUR)", _de(syn["total_eur"])],
             ["  davon eingesparte Netzkosten (EUR)", _de(syn["grid_savings_eur"])],
             ["  davon eingesparte Entwicklungskosten (EUR)", _de(syn["development_savings_eur"])]]
    return [
        Paragraph("Anlage 1 – Flächen", ST_H), _table(rows1, [5.0, 5.8, 5.8]),
        Spacer(1, 10),
        Paragraph("Anlage 2 – Berechnung der Beteiligungsquoten (Vorplanung)", ST_H),
        _table(rows3, [10.0, 6.6]), Spacer(1, 8), _table(rows2, [6.2, 5.2, 5.2]),
        Spacer(1, 6),
        Paragraph(escape("Kapitalwerte: Barwert über die Laufzeit abzüglich Investition, Netzanschluss und Entwicklungskosten; "
                         f"Zinssatz {_de(plan['assumptions']['discount_rate'] * 100, 1)} %, {plan['assumptions']['lifetime_years']} Jahre. "
                         "Standardannahmen (Screening-Modell), kein bankfähiges Finanzmodell."), ST_SMALL),
    ]


def build_pool_agreement_pdf(plan: dict[str, Any], package: dict[str, Any], params: DocParams) -> bytes:
    pa, pb = (_party(p) for p in package["parties"])
    story: list = [
        Paragraph("Flächenpoolvertrag", ST_TITLE),
        Paragraph(f"Kooperations- und Poolvereinbarung · ENTWURF · Vorlage {POOL_TEMPLATE}", ST_SUB),
        Paragraph("zwischen", ST_BODY),
        Paragraph(pa.block() + "<br/>– nachfolgend „<b>Partei A</b>“ –", ST_PARTY),
        Paragraph("und", ST_BODY),
        Paragraph(pb.block() + "<br/>– nachfolgend „<b>Partei B</b>“ –", ST_PARTY),
        Paragraph("– gemeinsam die „Parteien“ –", ST_BODY),
        Paragraph("Präambel", ST_H),
        Paragraph(escape(
            "Die Parteien wurden über die Plattform „" + params.platform_name + "“ unter der Vermittlungskennung " + params.match_id +
            " zusammengeführt und haben eine Geheimhaltungs- und Nichtumgehungsvereinbarung geschlossen. Sie wollen ihre benachbarten Flächen "
            "gemeinsam entwickeln. Der Plattformbetreiber ist nicht Partei dieses Vertrags und gibt keine Gewähr für die Berechnungen."), ST_BODY),
    ]
    story += _paras(_pool_sections(plan, pa, pb, params))
    story += [Spacer(1, 16), Paragraph("Unterschriften", ST_H),
              _table([["Partei A", "Partei B"],
                      [f"Ort, Datum\n\n\n______________________________\n{pa.company_name}\n{pa.representative}",
                       f"Ort, Datum\n\n\n______________________________\n{pb.company_name}\n{pb.representative}"]], [8.3, 8.3]),
              PageBreak()]
    story += _plan_tables(plan, package)
    story += [Spacer(1, 6), Paragraph("Anlage 3 – Netzanschlussbegehren (gesondertes Dokument)", ST_H)]
    if plan["warnings"]:
        story += [Paragraph("Hinweise zur Berechnung", ST_H)] + [Paragraph(escape("• " + w), ST_ITEM) for w in plan["warnings"]]
    story.append(Paragraph(escape("Dieses Dokument ist ein maschinell erzeugter Entwurf und ersetzt keine Rechtsberatung. Vor Unterzeichnung "
                                  "ist eine Prüfung durch eine in Deutschland zugelassene Rechtsanwältin bzw. einen Rechtsanwalt sowie steuerliche "
                                  "Beratung erforderlich."), ST_SMALL))
    return _build(story, "Flächenpoolvertrag (Entwurf)", POOL_TEMPLATE, params.pool_id, params, "Entwurf")


# ================================================================== Netzanschlussbegehren


def build_grid_application_pdf(plan: dict[str, Any], package: dict[str, Any], params: DocParams,
                               operator: Optional[str] = None, substation: Optional[str] = None) -> bytes:
    pa, pb = (_party(p) for p in package["parties"])
    jp = plan["joint_project"]
    wind = jp["wind_turbines"]
    n_turb = sum(len(v) for v in wind.values())
    fa, fb = package["parties"][0]["parcel"], package["parties"][1]["parcel"]
    techs = sorted({p["parcel"]["technology"] for p in plan["parties"]})
    kind = " und ".join(_tech(t) for t in techs)
    date = params.created_at.astimezone(timezone.utc).strftime("%d.%m.%Y")

    story: list = [
        Paragraph(escape(f"{pa.company_name} · {pb.company_name}"), ST_SMALL),
        Spacer(1, 10),
        Paragraph(escape(operator or f"{PLACEHOLDER} (Netzbetreiber)") + "<br/>Netzanschlussmanagement", ST_BODY),
        Spacer(1, 8),
        Paragraph(escape(date), ST_BODY),
        Paragraph("Netzanschlussbegehren für eine Erzeugungsanlage – Anfrage zur Netzverträglichkeitsprüfung", ST_TITLE),
        Paragraph(escape(f"{kind} · ca. {_de(jp['capacity_mw'], 1)} MW · {fa['municipality'] or ''} · Vorlage {GRID_TEMPLATE}"), ST_SUB),
        Paragraph("Sehr geehrte Damen und Herren,", ST_BODY),
        Paragraph(escape(
            f"wir planen gemeinsam die Errichtung und den Betrieb einer Erzeugungsanlage ({kind}) und beantragen hiermit den Anschluss an Ihr Netz "
            "sowie die Einspeisung des erzeugten Stroms nach § 8 des Erneuerbare-Energien-Gesetzes (EEG 2023) und § 17 des "
            "Energiewirtschaftsgesetzes (EnWG). Wir bitten um Prüfung der Netzverträglichkeit, die Benennung des Netzverknüpfungspunkts, "
            "eine Netzanschlusszusage mit Kostenschätzung und die unverzügliche Übermittlung eines Zeitplans für die weitere Bearbeitung. "
            "Soweit Sie für den Antrag eigene Formulare nach VDE-AR-N 4110 bzw. 4120 verwenden, bitten wir um Zusendung; die nachstehenden "
            "Angaben können dort übernommen werden."), ST_BODY),
        Paragraph("1. Antragsteller", ST_H),
        Paragraph(pa.block() + "<br/>– Partei A –", ST_PARTY),
        Paragraph(pb.block() + "<br/>– Partei B –", ST_PARTY),
        Paragraph(escape("Die Antragsteller bilden eine Gesellschaft bürgerlichen Rechts in Gründung; eine Projektgesellschaft wird "
                         "gegründet und tritt in das Verfahren ein. Ansprechpartner für Rückfragen: " + (pa.email or PLACEHOLDER) + "."), ST_BODY),
        Paragraph("2. Anlagendaten (Vorplanung)", ST_H),
        _table([
            ["Anlagenart", kind], ["Installierte Leistung", f"ca. {_de(jp['capacity_mw'], 2)} MW"],
            ["Erwartete Jahreserzeugung", f"ca. {_de(jp['energy_mwh_a'])} MWh/a"],
            ["Windenergieanlagen", f"{n_turb} Stück à {_de(plan['assumptions']['wind_rated_mw'], 1)} MW, Rotordurchmesser "
                                   f"{_de(plan['assumptions']['wind_rotor_diameter_m'])} m" if n_turb else "-"],
            ["Photovoltaik", "; ".join(f"Partei {p['label']}: {_de(p['in_joint_project']['capacity_mw'], 2)} MWp"
                                       for p in plan["parties"] if p["parcel"]["technology"] == "solar") or "-"],
            ["Gewünschte Anschlussebene", "Mittel- oder Hochspannung nach Netzbetreiberprüfung (VDE-AR-N 4110/4120)"],
            ["Gewünschte Inbetriebnahme", PLACEHOLDER],
            ["Vorgesehener Verknüpfungspunkt (Vorschlag)", (substation or "nächstgelegenes Umspannwerk laut OpenStreetMap") +
             (f", Entfernung ca. {_de(min(p['parcel']['distance_to_substation_m'] for p in plan['parties'] if p['parcel']['distance_to_substation_m'] is not None))} m"
              if any(p["parcel"]["distance_to_substation_m"] is not None for p in plan["parties"]) else "")],
        ], [5.5, 11.1], header=False),
        Paragraph("3. Standort", ST_H),
        _table([["Fläche", "Gemeinde / Bundesland", "Flurstücke (ALKIS)", "Fläche (ha)"],
                ["Partei A", f"{fa['municipality'] or '-'} / {fa['bundesland'] or '-'}", fa.get("alkis_id") or "-", _de(fa["area_ha"], 2)],
                ["Partei B", f"{fb['municipality'] or '-'} / {fb['bundesland'] or '-'}", fb.get("alkis_id") or "-", _de(fb["area_ha"], 2)]],
               [2.2, 6.0, 5.8, 2.6]),
    ]
    if n_turb:
        rows = [["Anlage", "Partei", "Breite (WGS84)", "Länge (WGS84)"]]
        i = 1
        for lab, pts in wind.items():
            for p in pts:
                rows.append([f"WEA {i}", lab, _de(p["lat"], 6), _de(p["lon"], 6)])
                i += 1
        story += [Spacer(1, 6), Paragraph(escape("Vorläufige Anlagenstandorte (Planungsstand, nicht genehmigt):"), ST_BODY), _table(rows, [3.0, 2.5, 5.5, 5.6])]
    story += [
        Paragraph("4. Bitte um Auskunft", ST_H),
        Paragraph(escape("a) Netzverknüpfungspunkt und Anschlussebene, b) verfügbare Einspeisekapazität und erforderlicher Netzausbau, "
                         "c) voraussichtliche Anschlusskosten und Baukostenzuschuss, d) Zeitplan für Netzverträglichkeitsprüfung, Netzanschlusszusage "
                         "und Realisierung, e) Ihre technischen Anschlussbedingungen und einzureichenden Unterlagen, f) Hinweise zu "
                         "Einspeisemanagement und Redispatch."), ST_BODY),
        Paragraph("5. Hinweise", ST_H),
        Paragraph(escape("Die Angaben beruhen auf einer überschlägigen Vorplanung; Leistung, Anlagenzahl und Standorte können sich im "
                         "Genehmigungsverfahren ändern. Wir bitten um schriftliche Eingangsbestätigung. Die Vollmacht der unterzeichnenden "
                         "Personen kann auf Wunsch nachgereicht werden."), ST_BODY),
        Paragraph("Mit freundlichen Grüßen", ST_BODY),
        Spacer(1, 8),
        _table([["Partei A", "Partei B"],
                [f"\n\n______________________________\n{pa.company_name}\n{pa.representative}",
                 f"\n\n______________________________\n{pb.company_name}\n{pb.representative}"]], [8.3, 8.3]),
        Spacer(1, 10),
        Paragraph(escape(f"Anlagen: Lageplan/Flächengrenzen (aus dem Flächenaudit, Vermittlung {params.match_id}); Übersichtsplan mit "
                         "Anlagenstandorten [bitte beifügen]; Vollmachten [bitte beifügen]."), ST_SMALL),
        Paragraph(escape("Maschinell erzeugter Entwurf; vor Versand inhaltlich prüfen und Platzhalter in eckigen Klammern ergänzen."), ST_SMALL),
    ]
    return _build(story, "Netzanschlussbegehren (Entwurf)", GRID_TEMPLATE, params.pool_id, params, "Entwurf")
