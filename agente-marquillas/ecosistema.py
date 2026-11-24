"""
Avisos al ecosistema 3D de Marquillas Store (POST /ws/ecosistema-evento).

Es SOLO un aviso visual de en qué va el agente: nunca bloquea, nunca reintenta y
nunca lanza excepciones hacia el agente. Cada envío sale en un hilo daemon con
timeout de 2 s y cualquier error se descarta (como mucho un log en debug).

Uso desde el ciclo (una línea por llamada):
    ecosistema.iniciar(total)                         -> paso "revisando"
    ecosistema.paso("validando", None, actual, total) -> "Factura 2 de 5"
    ecosistema.paso("verificando")                    -> reusa actual/total del documento en curso
    ecosistema.terminar(ok, procesadas)               -> "listo" o "error" y apaga el latido

Se desactiva en silencio si ECOSISTEMA_TOKEN está vacío o si no hay URL base
(se toma de API_FACTURAS_URL, la misma que usa /ws/recibir-facturas).

IMPORTANTE: mensaje y datos solo llevan conteos y el tipo de paso — se muestran
en una pantalla compartida (nada de proveedores, NIT, números de factura ni montos).
"""

import logging
import os
import threading
import time
from urllib.parse import urlsplit

import requests

AGENTE            = "doc_almacenes"
RUTA_EVENTO       = "/ws/ecosistema-evento"
TIMEOUT_SEGUNDOS  = 2
LATIDO_SEGUNDOS   = 20

_log = logging.getLogger("ecosistema")
_log.addHandler(logging.NullHandler())
_log.propagate = False  # No mezclarse con los logs del agente (monitor-agentes los lee)

_candado = threading.Lock()
_estado = {
    "activo":   False,
    "url":      "",
    "token":    "",
    "episodio": 0,
    "evento":   None,   # último evento enviado — lo reenvía el latido
    "actual":   0,
    "total":    0,
    "parar":    None,   # threading.Event del latido en curso
}


def _url_base() -> str:
    """URL base de Marquillas Store a partir de API_FACTURAS_URL (lo que va antes de /ws/)."""
    url = os.getenv("API_FACTURAS_URL", "").strip()
    if not url:
        return ""
    if "/ws/" in url:
        return url.split("/ws/")[0].rstrip("/")
    partes = urlsplit(url)
    return f"{partes.scheme}://{partes.netloc}" if partes.scheme and partes.netloc else ""


def _post(url: str, token: str, cuerpo: dict) -> None:
    """Un único intento, timeout corto; cualquier fallo se traga."""
    try:
        requests.post(
            url,
            json=cuerpo,
            headers={"X-Ecosistema-Token": token},
            timeout=TIMEOUT_SEGUNDOS,
        )
    except Exception as error:
        try:
            _log.debug(f"ecosistema: envío fallido ({type(error).__name__})")
        except Exception:
            pass


def _enviar(url: str, token: str, cuerpo: dict) -> None:
    """Lanza el POST en un hilo daemon para no frenar nunca al agente."""
    try:
        threading.Thread(target=_post, args=(url, token, cuerpo), daemon=True).start()
    except Exception:
        pass


def _latido(parar: threading.Event) -> None:
    """Reenvía el último evento cada LATIDO_SEGUNDOS mientras el ciclo esté abierto."""
    try:
        while not parar.wait(LATIDO_SEGUNDOS):
            with _candado:
                if not _estado["activo"] or _estado["parar"] is not parar or not _estado["evento"]:
                    return
                url, token, cuerpo = _estado["url"], _estado["token"], dict(_estado["evento"])
            _enviar(url, token, cuerpo)
    except Exception:
        pass


def _registrar_y_enviar(paso: str, mensaje: str, datos: dict) -> None:
    """Guarda el evento como 'último' (para el latido) y lo envía. Requiere ciclo activo."""
    with _candado:
        if not _estado["activo"]:
            return
        cuerpo = {
            "agente":   AGENTE,
            "episodio": _estado["episodio"],
            "paso":     paso,
            "mensaje":  mensaje,
            "datos":    datos,
        }
        _estado["evento"] = cuerpo
        url, token = _estado["url"], _estado["token"]
    _enviar(url, token, dict(cuerpo))


def iniciar(total: int) -> None:
    """Abre el episodio del ciclo, envía 'revisando' y arranca el latido."""
    try:
        token = os.getenv("ECOSISTEMA_TOKEN", "").strip()
        base  = _url_base()
        with _candado:
            if _estado["parar"] is not None:
                _estado["parar"].set()  # Por si quedó un latido de un ciclo anterior
            _estado.update(activo=False, evento=None, parar=None, actual=0, total=0)
            if not token or not base:
                return  # Desactivado en silencio
            parar = threading.Event()
            _estado.update(
                activo=True,
                url=base + RUTA_EVENTO,
                token=token,
                episodio=int(time.time() * 1000),
                total=int(total),
                parar=parar,
            )
        _registrar_y_enviar("revisando", f"Revisando {int(total)} correos", {"actual": 0, "total": int(total)})
        threading.Thread(target=_latido, args=(parar,), daemon=True).start()
    except Exception:
        pass


def paso(paso: str, mensaje: str | None = None, actual: int | None = None, total: int | None = None) -> None:
    """
    Avisa la etapa del documento en curso: validando, verificando, clasificando,
    archivando o notificando. Si no se pasan actual/total se reusan los del documento
    en curso; si no se pasa mensaje se arma "Factura {actual} de {total}".
    """
    try:
        with _candado:
            if not _estado["activo"]:
                return
            if actual is not None:
                _estado["actual"] = int(actual)
            if total is not None:
                _estado["total"] = int(total)
            actual_, total_ = _estado["actual"], _estado["total"]
        _registrar_y_enviar(
            paso,
            mensaje or f"Factura {actual_} de {total_}",
            {"actual": actual_, "total": total_},
        )
    except Exception:
        pass


def terminar(ok: bool, procesadas: int | None = None) -> None:
    """Envía 'listo' o 'error' y apaga el latido. Sin ciclo abierto no hace nada."""
    try:
        with _candado:
            if not _estado["activo"]:
                return
            if procesadas is None:
                procesadas = _estado["actual"]
            total = _estado["total"]
        if ok:
            _registrar_y_enviar("listo", f"Ciclo terminado: {int(procesadas)} procesadas",
                                {"actual": int(procesadas), "total": total})
        else:
            _registrar_y_enviar("error", "Error en el ciclo",
                                {"actual": _estado["actual"], "total": total})
        with _candado:
            if _estado["parar"] is not None:
                _estado["parar"].set()
            _estado.update(activo=False, evento=None, parar=None)
    except Exception:
        pass
