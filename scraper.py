#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CVC - Robot de actualizacion del parte de transitabilidad + titulares i24
============================================================================
Corre en GitHub Actions. No necesita nada local.

BLINDADO: rutas y titulares se actualizan por separado. Si falla el parte de
Vialidad, los titulares i24 se actualizan igual (y viceversa); lo que falle
mantiene el ultimo dato bueno. Siempre deja un resumen legible + diagnostico
tecnico en resumen.md (visible en Actions).
"""

import re
import io
import sys
import json
import unicodedata
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

try:
    import pdfplumber
except Exception:
    pdfplumber = None


# ============================================================
# CONFIG  --  editar aca sin tocar el resto del codigo
# ============================================================
AGVP_BASE = "https://www.agvp.gob.ar/PartesDiarios/"
FUENTES = {
    "Nacional":   {"htm": AGVP_BASE + "PartesNacionales.htm",
                   "pdf": AGVP_BASE + "PartesNacionales.pdf"},
    "Provincial": {"htm": AGVP_BASE + "PartesProvinciales.htm",
                   "pdf": AGVP_BASE + "PartesProvinciales.pdf"},
}
I24_URL = "https://i24.com.ar/"

CORREDOR_NORTE = {
    "Nacional":   {"3", "40", "281"},
    "Provincial": {"12", "43", "47", "39", "49", "99", "16", "18", "41", "14"},
}
ESTADOS_SIEMPRE = {"CORTADO", "RESTRINGIDO"}
MAX_FILAS = 18
CANT_AVISOS = 10
# Titulares i24: se toman los mas NUEVOS (por id de nota en la URL).
AVISOS_VENTANA = 16          # ventana de las N notas mas nuevas de donde elegir
PRIORIZAR_LOCALES = False    # False = estrictamente las mas nuevas (sin sesgo local)
#   (poner True = dentro de lo reciente, poner primero las locales de Santa Cruz)
LOCALES = ["santa cruz", "caleta", "caleta olivia", "rio gallegos", "río gallegos",
           "el calafate", "calafate", "las heras", "pico truncado", "perito moreno",
           "puerto deseado", "puerto san julian", "san julian", "gallegos",
           "provincial", "grasso", "vidal", "gobernador", "canadon seco", "cañadón seco",
           "28 de noviembre", "rio turbio", "río turbio", "los antiguos", "chalten", "chaltén"]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
}
TZ_AR = timezone(timedelta(hours=-3))

MESES = {m: i + 1 for i, m in enumerate(
    ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
     "agosto", "septiembre", "setiembre", "octubre", "noviembre", "diciembre"])}
MESES["setiembre"] = 9

OBS_TRIGGERS = ["Calzada", "Banquina", "Banquinas", "Presencia", "Animales", "Solo",
                "Sin datos", "Acumulaci", "Visibilidad", "Equipos", "Equipo", "Camioneta",
                "Desv", "Distribuci", "Portaci", "Guardaganado", "Despeje", "Restricci",
                "Restringida", "Reserva", "Vientos", "Baches", "Nieve"]
ESTADO_WORDS = ("HABILITADA", "RESTRINGIDA", "CORTE TOTAL")

DIAG_I24 = []   # diagnostico de los titulares (se muestra en el Summary)


# ============================================================
# Utilidades
# ============================================================
def norm(s):
    s = (s or "").strip()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s).lower()


def limpiar(s):
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def http_get(url, binary=False):
    params = {"_": int(datetime.now().timestamp())}
    r = requests.get(url, headers=HEADERS, params=params, timeout=45)
    r.raise_for_status()
    if binary:
        return r.content
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def leer_fecha(texto):
    t = limpiar(texto)
    md = re.search(r"D[ií]a\s+\w+,?\s*(\d{1,2})\s+de\s+([A-Za-zÁ-úá-ú]+)\s+de\s+(\d{4})", t, re.I)
    mh = re.search(r"Hora\s*([0-2]?\d[:\.][0-5]\d)", t, re.I)
    if not md:
        return None, ""
    dia = int(md.group(1))
    mes = MESES.get(norm(md.group(2)), 1)
    anio = int(md.group(3))
    hora = (mh.group(1).replace(".", ":") if mh else "00:00")
    try:
        hh, mm = [int(x) for x in hora.split(":")[:2]]
    except Exception:
        hh, mm = 0, 0
    try:
        dt = datetime(anio, mes, dia, hh, mm, tzinfo=TZ_AR)
    except Exception:
        return None, ""
    return dt, f"{dia:02d}/{mes:02d} {hh:02d}:{mm:02d}"


def mapear_estado(estado_raw, transit_raw):
    e = norm(estado_raw)
    tr = norm(transit_raw)
    if "corte total" in e or "corte total" in tr:
        return "CORTADO"
    if "restring" in e or "restring" in tr:
        return "RESTRINGIDO"
    if "intransitable" in tr:
        return "RESTRINGIDO"
    if "extrema" in tr:
        return "EXTREMA"
    if "precau" in tr:
        return "PRECAUCION"
    if "sin datos" in tr:
        return "INFORMACION"
    if "habilitada" in e:
        return "NORMAL"
    return "INFORMACION"


def separar_tramo_obs(txt):
    txt = limpiar(txt)
    pos = len(txt)
    for w in OBS_TRIGGERS:
        m = re.search(r"\b" + re.escape(w), txt)
        if m and m.start() < pos:
            pos = m.start()
    return limpiar(txt[:pos]), limpiar(txt[pos:])


def leer_calzada(txt):
    m = re.search(r"Est\.?\s*calzada:\s*(Bueno\s*-\s*Regular|Regular\s*-\s*Malo|Bueno|Regular|Malo)",
                  txt or "", re.I)
    return m.group(1).replace(" ", "") if m else ""


def quitar_transit(txt):
    txt = re.sub(r"Transitable con (?:extrema )?precauci[oó]n\.?", " ", txt or "")
    txt = re.sub(r"\bIntransitable\b", " ", txt)
    return limpiar(txt)


# ============================================================
# Parser HTML  (metodo 1: por texto plano; metodo 2: por celdas)
# ============================================================
def _fila_desde_bloque(nro, tipo, tramo_ini, estado_raw, resto):
    """Arma una fila a partir de las piezas ya separadas."""
    transit_raw = resto
    estado = mapear_estado(estado_raw, transit_raw)
    calzada = leer_calzada(resto)
    mkm = re.search(r"\(\s*\d+\s+kil[óo]metros", resto, re.I)
    corte = mkm.start() if mkm else -1
    destino_zone = resto[:corte] if corte > 0 else resto
    destino_zone = quitar_transit(destino_zone)
    tramo_fin, obs2 = separar_tramo_obs(destino_zone)
    obs_pre = ""
    # observaciones que aparecen antes del destino (justo tras el estado)
    pre_zone = quitar_transit(resto[:resto.find(tramo_fin)] if tramo_fin and tramo_fin in resto else "")
    obs_pre = pre_zone
    obs3 = ""
    if calzada:
        idx = resto.lower().find("calzada:")
        if idx >= 0:
            tail = re.sub(r"^\s*(Bueno\s*-\s*Regular|Regular\s*-\s*Malo|Bueno|Regular|Malo)",
                          "", resto[idx + 8:], flags=re.I)
            obs3 = limpiar(tail)
    tramo = tramo_ini
    if tramo_fin and norm(tramo_fin) != norm(tramo_ini):
        tramo = f"{tramo_ini} / {tramo_fin}"
    obs = limpiar(" ".join([obs_pre, obs2, obs3]))
    obs = re.sub(r"\s*\.\s*\.", ".", obs).strip(" .") or "Sin novedades."
    return {
        "ruta": f"{'RN' if tipo == 'Nacional' else 'RP'} {nro}",
        "_nro": nro, "tipo": tipo, "tramo": limpiar(tramo),
        "estado": estado, "calzada": calzada or "-", "observaciones": obs,
    }


def filas_desde_htm(texto_html, tipo):
    """Metodo 1: agrupa el texto plano por ancla de ruta (una linea 'Nac./Prov. N X')."""
    soup = BeautifulSoup(texto_html, "lxml")
    texto = soup.get_text("\n")
    fecha_dt, fecha_leg = leer_fecha(texto)
    lineas = [limpiar(l) for l in texto.split("\n") if limpiar(l)]
    ancla = re.compile(r"^(Nac|Prov)\.?\s*N[°ºo]\s*\d+\b", re.I)
    registros, actual = [], None
    for l in lineas:
        if ancla.match(l):
            if actual:
                registros.append(actual)
            actual = [l]
        elif actual is not None:
            actual.append(l)
    if actual:
        registros.append(actual)
    filas = []
    for reg in registros:
        texto_seg = " ".join(reg)
        m = re.match(r"^(Nac|Prov)\.?\s*N[°ºo]\s*(\d+)\s+(.*?)\s+(HABILITADA|RESTRINGIDA|CORTE TOTAL)\b(.*)$",
                     texto_seg, re.I | re.S)
        if not m:
            continue
        f = _fila_desde_bloque(m.group(2), tipo, limpiar(m.group(3)),
                               m.group(4).upper(), limpiar(m.group(5)))
        if f:
            filas.append(f)
    return fecha_dt, fecha_leg, filas


def filas_desde_htm_celdas(texto_html, tipo):
    """Metodo 2: lee las filas de la <table> celda por celda (mas fiel al Excel)."""
    soup = BeautifulSoup(texto_html, "lxml")
    fecha_dt, fecha_leg = leer_fecha(soup.get_text(" "))
    trs = []
    for tr in soup.find_all("tr"):
        celdas = [limpiar(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
        celdas = [c for c in celdas if c]
        if celdas:
            trs.append(celdas)
    # unir todo el texto por filas y volver a agrupar por ancla
    blob = "  ".join("  ".join(c) for c in trs)
    ancla = re.compile(r"(Nac|Prov)\.?\s*N[°ºo]\s*\d+", re.I)
    partes = list(ancla.finditer(blob))
    filas = []
    for i, mm in enumerate(partes):
        ini = mm.start()
        fin = partes[i + 1].start() if i + 1 < len(partes) else len(blob)
        seg = blob[ini:fin]
        m = re.match(r"^(Nac|Prov)\.?\s*N[°ºo]\s*(\d+)\s+(.*?)\s+(HABILITADA|RESTRINGIDA|CORTE TOTAL)\b(.*)$",
                     seg, re.I | re.S)
        if not m:
            continue
        f = _fila_desde_bloque(m.group(2), tipo, limpiar(m.group(3)),
                               m.group(4).upper(), limpiar(m.group(5)))
        if f:
            filas.append(f)
    return fecha_dt, fecha_leg, filas


def filas_desde_htm_columnas(texto_html, tipo):
    """Metodo 3 (preferido): agrupa cada segmento en sus filas de tabla y respeta
       columnas. Estructura tipica del Excel: 3 <tr> por tramo:
         fila0 = [RUTA, TRAMO_INICIO, ESTADO, TRANSITABILIDAD, REF1]
         fila1 = [TRAMO_FIN, REF2]
         fila2 = [( N km de tipo ), Est. calzada: X, REF3]"""
    soup = BeautifulSoup(texto_html, "lxml")
    fecha_dt, fecha_leg = leer_fecha(soup.get_text(" "))
    trs = []
    for tr in soup.find_all("tr"):
        cells = [limpiar(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if cells:
            trs.append(cells)
    ancla = re.compile(r"^(Nac|Prov)\.?\s*N[°ºo]\s*\d+", re.I)
    segmentos, actual = [], None
    for cells in trs:
        if ancla.match(cells[0]):
            if actual:
                segmentos.append(actual)
            actual = [cells]
        elif actual is not None:
            actual.append(cells)
    if actual:
        segmentos.append(actual)
    filas = []
    for seg in segmentos:
        f = _parsear_segmento_columnas(seg, tipo)
        if f:
            filas.append(f)
    return fecha_dt, fecha_leg, filas


def _es_estado(c):
    cu = c.upper()
    return any(w in cu for w in ESTADO_WORDS)


def _parsear_segmento_columnas(seg, tipo):
    row0 = seg[0]
    m = re.match(r"(Nac|Prov)\.?\s*N[°ºo]\s*(\d+)", row0[0], re.I)
    if not m:
        return None
    nro = m.group(2)
    refs = []
    estado_idx = next((i for i, c in enumerate(row0) if _es_estado(c)), None)
    if estado_idx is not None:
        estado_raw = row0[estado_idx]
        tramo_ini = " ".join(row0[1:estado_idx]) if estado_idx > 1 else (row0[1] if len(row0) > 1 else "")
        transit = ""
        ref_start = estado_idx + 1
        if len(row0) > estado_idx + 1 and re.match(r"(Transitable|Intransitable)", row0[estado_idx + 1], re.I):
            transit = row0[estado_idx + 1]
            ref_start = estado_idx + 2
        refs.extend(row0[ref_start:])
    else:
        estado_raw = ""
        tramo_ini = row0[1] if len(row0) > 1 else ""
        transit = ""

    tramo_fin, calzada = "", ""
    for cells in seg[1:]:
        joined = " ".join(cells)
        if re.search(r"\(\s*\d+\s+kil[óo]metros", joined, re.I) or "calzada:" in joined.lower():
            calzada = leer_calzada(joined) or calzada
            for c in cells:
                if re.search(r"\(\s*\d+\s+kil[óo]metros", c, re.I):
                    continue
                if "calzada:" in c.lower():
                    continue
                refs.append(c)
        else:
            if not tramo_fin and cells:
                tramo_fin = cells[0]
                refs.extend(cells[1:])
            else:
                refs.extend(cells)

    estado = mapear_estado(estado_raw, transit + " " + " ".join(refs))
    tramo = limpiar(tramo_ini)
    if tramo_fin and norm(tramo_fin) != norm(tramo_ini):
        tramo = f"{limpiar(tramo_ini)} / {limpiar(tramo_fin)}"
    obs = limpiar(" ".join(refs)).strip(" .") or "Sin novedades."
    return {"ruta": f"{'RN' if tipo == 'Nacional' else 'RP'} {nro}", "_nro": nro,
            "tipo": tipo, "tramo": tramo, "estado": estado,
            "calzada": calzada or "-", "observaciones": obs}


def estructura_htm(texto_html, n=16):
    """Devuelve las primeras n filas de tabla como listas de celdas (para diagnostico)."""
    soup = BeautifulSoup(texto_html, "lxml")
    out = []
    for tr in soup.find_all("tr"):
        celdas = [limpiar(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
        celdas = [c for c in celdas if c]
        if celdas:
            out.append(celdas)
        if len(out) >= n:
            break
    return out


# ============================================================
# Parser PDF (por coordenadas)
# ============================================================
def filas_desde_pdf(pdf_bytes, tipo):
    if not pdfplumber:
        return None, "", []
    fecha_dt, fecha_leg = None, ""
    lineas = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            if not words:
                continue
            if not fecha_dt:
                fdt, fleg = leer_fecha(page.extract_text() or "")
                if fdt:
                    fecha_dt, fecha_leg = fdt, fleg
            cols = _detectar_columnas(words)
            lineas.extend(_agrupar_filas(words, cols))
    registros, actual = [], None
    ancla = re.compile(r"(Nac|Prov)\.?\s*N[°ºo]?\s*\d+", re.I)
    for fila in lineas:
        if ancla.search(fila.get("ruta", "")):
            if actual:
                registros.append(actual)
            actual = {"ruta": fila.get("ruta", ""), "_tramos": [fila.get("tramo", "")],
                      "_refs": [fila.get("refs", "")], "estado": fila.get("estado", ""),
                      "transit": fila.get("transit", "")}
        elif actual is not None:
            actual["_tramos"].append(fila.get("tramo", ""))
            actual["_refs"].append(fila.get("refs", ""))
            for k in ("estado", "transit"):
                if not actual.get(k) and fila.get(k):
                    actual[k] = fila[k]
    if actual:
        registros.append(actual)
    filas = []
    for reg in registros:
        m = re.search(r"(Nac|Prov)\.?\s*N[°ºo]?\s*(\d+)", reg["ruta"], re.I)
        if not m:
            continue
        tramos = [t for t in reg["_tramos"] if t]
        tramo_ini = tramos[0] if tramos else ""
        tramo_fin, _ = separar_tramo_obs(tramos[1] if len(tramos) > 1 else "")
        refs = limpiar(" ".join(reg["_refs"]))
        estado = mapear_estado(reg.get("estado", ""), reg.get("transit", "") + " " + refs)
        calzada = leer_calzada(refs) or leer_calzada(" ".join(reg["_tramos"]))
        obs = re.sub(r"\(\s*\d+\s+kil[óo]metros[^)]*\)", "", refs)
        obs = re.sub(r"Est\.?\s*calzada:\s*(Bueno\s*-\s*Regular|Regular\s*-\s*Malo|Bueno|Regular|Malo)",
                     "", obs, flags=re.I)
        obs = limpiar(obs) or "Sin novedades."
        tramo = tramo_ini
        if tramo_fin and norm(tramo_fin) != norm(tramo_ini):
            tramo = f"{tramo_ini} / {tramo_fin}"
        filas.append({"ruta": f"{'RN' if tipo == 'Nacional' else 'RP'} {m.group(2)}",
                      "_nro": m.group(2), "tipo": tipo, "tramo": limpiar(tramo),
                      "estado": estado, "calzada": calzada or "-", "observaciones": obs})
    return fecha_dt, fecha_leg, filas


def _detectar_columnas(words):
    heads = {"ruta": None, "tramo": None, "estado": None, "transit": None, "refs": None}
    filas = {}
    for w in words:
        filas.setdefault(round(w["top"]), []).append(w)
    for _, ws in filas.items():
        txt = norm(" ".join(x["text"] for x in ws))
        if "ruta" in txt and "estado" in txt and ("tramo" in txt or "datos" in txt):
            for x in ws:
                t = norm(x["text"])
                if t == "ruta" and heads["ruta"] is None:
                    heads["ruta"] = x["x0"]
                elif t.startswith("datos") and heads["tramo"] is None:
                    heads["tramo"] = x["x0"]
                elif t == "estado" and heads["estado"] is None:
                    heads["estado"] = x["x0"]
                elif t.startswith("transitab") and heads["transit"] is None:
                    heads["transit"] = x["x0"]
                elif t.startswith("referencia") and heads["refs"] is None:
                    heads["refs"] = x["x0"]
            break
    if heads["ruta"] is None:
        heads = {"ruta": 0, "tramo": 70, "estado": 300, "transit": 380, "refs": 520}
    return heads


def _agrupar_filas(words, cols):
    filas = {}
    for w in words:
        filas.setdefault(round(w["top"] / 3.0), []).append(w)
    bordes = sorted([(cols[k], k) for k in cols])
    salida = []
    for _, ws in sorted(filas.items()):
        celdas = {k: [] for k in cols}
        for w in ws:
            col = bordes[0][1]
            for x0, k in bordes:
                if w["x0"] >= x0 - 5:
                    col = k
            celdas[col].append((w["x0"], w["text"]))
        row = {k: limpiar(" ".join(t for _, t in sorted(v))) for k, v in celdas.items()}
        if any(row.values()):
            salida.append(row)
    return salida


def muestra_pdf(pdf_bytes, n=1600):
    if not pdfplumber:
        return "(pdfplumber no disponible)"
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return (pdf.pages[0].extract_text() or "")[:n]
    except Exception as e:
        return f"(error leyendo pdf: {e})"


# ============================================================
# Orquestacion de un parte, con diagnostico
# ============================================================
def obtener_parte(tipo, urls):
    diag = [f"### {tipo}"]
    candidatos = []
    muestras = {}

    # ---- HTML ----
    try:
        txt = http_get(urls["htm"])
        fdt0, fleg0, filas0 = filas_desde_htm_columnas(txt, tipo)
        fdt1, fleg1, filas1 = filas_desde_htm(txt, tipo)
        fdt2, fleg2, filas2 = filas_desde_htm_celdas(txt, tipo)
        # preferir el metodo por columnas; si da 0, usar el siguiente que traiga filas
        opciones = [("columnas", fdt0, fleg0, filas0),
                    ("celdas", fdt2, fleg2, filas2),
                    ("texto", fdt1, fleg1, filas1)]
        metodo, fdt, fleg, filas = next(((n, a, b, c) for n, a, b, c in opciones if c), opciones[0])
        diag.append(f"- htm: HTTP OK, {len(txt)} bytes, fecha {fleg or 's/f'}, {len(filas)} tramos "
                    f"(metodo {metodo}; columnas={len(filas0)}, celdas={len(filas2)}, texto={len(filas1)})")
        muestras["estructura_htm"] = estructura_htm(txt)
        if filas:
            candidatos.append(("htm", fdt, fleg, filas))
    except Exception as e:
        diag.append(f"- htm: ERROR {type(e).__name__}: {e}")

    # ---- PDF ----
    try:
        pdfb = http_get(urls["pdf"], binary=True)
        fdt, fleg, filas = filas_desde_pdf(pdfb, tipo)
        diag.append(f"- pdf: HTTP OK, {len(pdfb)} bytes, fecha {fleg or 's/f'}, {len(filas)} tramos")
        muestras["muestra_pdf"] = muestra_pdf(pdfb)
        if filas:
            candidatos.append(("pdf", fdt, fleg, filas))
    except Exception as e:
        diag.append(f"- pdf: ERROR {type(e).__name__}: {e}")

    if not candidatos:
        diag.append(f"- **RESULTADO: 0 tramos para {tipo}** (se mantiene el parte anterior).")
        return {"filas": [], "fecha_leg": "", "origen": "-", "diag": diag, "muestras": muestras}

    candidatos.sort(key=lambda r: (r[1] or datetime(1970, 1, 1, tzinfo=TZ_AR),
                                    1 if r[0] == "pdf" else 0), reverse=True)
    origen, fdt, fleg, filas = candidatos[0]
    diag.append(f"- fuente elegida: **{origen}** ({fleg or 's/f'}) - {len(filas)} tramos")
    return {"filas": filas, "fecha_leg": fleg, "origen": origen, "diag": diag, "muestras": muestras}


def curar(filas_nac, filas_prov):
    todas = filas_nac + filas_prov
    elegidas, vistas = [], set()

    def agregar(f):
        clave = (f["ruta"], norm(f["tramo"]))
        if clave in vistas:
            return
        vistas.add(clave)
        elegidas.append(f)

    for f in todas:
        if f.get("_nro", "") in CORREDOR_NORTE.get(f["tipo"], set()):
            agregar(f)
    for f in todas:
        if f["estado"] in ESTADOS_SIEMPRE:
            agregar(f)

    return [{k: v for k, v in f.items() if not k.startswith("_")} for f in elegidas[:MAX_FILAS]]


# ============================================================
# Titulares i24
# ============================================================
def obtener_avisos():
    try:
        txt = http_get(I24_URL)
    except Exception as e:
        print(f"[i24] error: {e}", file=sys.stderr)
        return []
    soup = BeautifulSoup(txt, "lxml")
    vistos, items = set(), []
    for a in soup.select('a[href*="/contenido/"]'):
        href = a.get("href", "")
        m = re.search(r"/contenido/(\d+)", href)
        if not m:
            continue
        cid = int(m.group(1))            # id de la nota: mas alto = mas nuevo
        titulo = limpiar(a.get_text())
        if len(titulo) < 25 or cid in vistos:
            continue
        vistos.add(cid)
        items.append((cid, titulo))
    if not items:
        return []

    # los mas NUEVOS primero (por id) y recortar a la ventana reciente
    items.sort(key=lambda x: x[0], reverse=True)
    ventana = [t for _, t in items[:AVISOS_VENTANA]]

    def es_local(t):
        n = norm(t)
        return any(k in n for k in LOCALES)

    if PRIORIZAR_LOCALES:
        ventana = [t for t in ventana if es_local(t)] + [t for t in ventana if not es_local(t)]

    orden, vistas_t = [], set()
    for t in ventana:
        if norm(t) in vistas_t:
            continue
        vistas_t.add(norm(t))
        orden.append(t)
        if len(orden) >= CANT_AVISOS:
            break
    return orden


# ============================================================
# Resumen / diagnostico (resumen.md -> se ve en Actions)
# ============================================================
def escribir_resumen(partes, salida, rutas_ok, avisos_ok):
    L = []
    L.append(f"**Corrida del robot:** {salida['actualizado']}  ")
    L.append("")
    if rutas_ok:
        L.append("## ✅ Rutas actualizadas")
        L.append(f"**Parte Vialidad:** Nacional {salida['fecha_parte_nacional']} · "
                 f"Provincial {salida['fecha_parte_provincial']}  ")
    else:
        L.append("## ⚠️ Rutas NO actualizadas (se mantiene el parte anterior)")
        L.append("No se pudo obtener/parsear el parte de Vialidad; la grafica sigue con el ultimo dato bueno.")
    L.append("")
    if salida["filas"]:
        L.append("| Ruta | Tramo | Estado |")
        L.append("|---|---|---|")
        for f in salida["filas"]:
            L.append(f"| {f['ruta']} | {f['tramo']} | {f['estado']} |")
    L.append("")
    if avisos_ok:
        L.append(f"## ✅ Titulares i24 actualizados ({len(salida['avisos'])})")
    else:
        L.append("## ⚠️ Titulares i24 NO actualizados (se mantienen los anteriores)")
    for a in salida["avisos"]:
        L.append(f"- {a}")
    L.append("")
    L.append("---")
    L.append("## Diagnostico tecnico")
    for tipo in ("Nacional", "Provincial"):
        p = partes.get(tipo, {})
        L.extend(p.get("diag", [f"### {tipo}", "- (sin datos)"]))
        m = p.get("muestras", {})
        if m.get("estructura_htm"):
            L.append("")
            L.append(f"<details><summary>Estructura HTML {tipo} (primeras filas, celda por celda)</summary>")
            L.append("")
            L.append("```")
            for i, celdas in enumerate(m["estructura_htm"]):
                L.append(f"[{i}] " + " | ".join(celdas))
            L.append("```")
            L.append("</details>")
        if m.get("muestra_pdf"):
            L.append("")
            L.append(f"<details><summary>Muestra PDF {tipo} (texto extraido)</summary>")
            L.append("")
            L.append("```")
            L.append(m["muestra_pdf"])
            L.append("```")
            L.append("</details>")
    with open("resumen.md", "w", encoding="utf-8") as f:
        f.write("\n".join(L))


# ============================================================
# Main (nunca falla el workflow salvo bug real)
# ============================================================
def main():
    partes = {tipo: obtener_parte(tipo, urls) for tipo, urls in FUENTES.items()}
    filas = curar(partes["Nacional"]["filas"], partes["Provincial"]["filas"])
    avisos = obtener_avisos()

    # Ultimo parte publicado (para no perder lo que ya estaba bien)
    try:
        with open("parte.json", encoding="utf-8") as f:
            previo = json.load(f)
    except Exception:
        previo = {}

    rutas_ok = bool(filas)
    avisos_ok = bool(avisos)

    # Rutas y titulares se actualizan POR SEPARADO: si uno falla,
    # se mantiene su ultimo dato bueno y el otro se actualiza igual.
    if not rutas_ok:
        filas = previo.get("filas", [])
    if not avisos_ok:
        avisos = previo.get("avisos", ["Informacion actualizada."])

    ahora = datetime.now(TZ_AR)
    salida = {
        "actualizado": ahora.strftime("%d/%m/%Y %H:%M"),
        "fecha_parte_nacional": (partes["Nacional"]["fecha_leg"]
                                 if rutas_ok and partes["Nacional"]["fecha_leg"]
                                 else previo.get("fecha_parte_nacional", "")),
        "fecha_parte_provincial": (partes["Provincial"]["fecha_leg"]
                                   if rutas_ok and partes["Provincial"]["fecha_leg"]
                                   else previo.get("fecha_parte_provincial", "")),
        "fuente_nacional": partes["Nacional"]["origen"] if rutas_ok else previo.get("fuente_nacional", "-"),
        "fuente_provincial": partes["Provincial"]["origen"] if rutas_ok else previo.get("fuente_provincial", "-"),
        "filas": filas,
        "avisos": avisos,
    }
    if filas or avisos:
        with open("parte.json", "w", encoding="utf-8") as f:
            json.dump(salida, f, ensure_ascii=False, indent=2)
    escribir_resumen(partes, salida, rutas_ok, avisos_ok)
    print(f"rutas: {'nuevas' if rutas_ok else 'se mantienen'} ({len(filas)}) | "
          f"titulares: {'nuevos' if avisos_ok else 'se mantienen'} ({len(avisos)})")
    # exit 0 siempre


if __name__ == "__main__":
    main()
