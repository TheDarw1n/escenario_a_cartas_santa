"""
Taller de Santa · API REST de Cartas (Escenario A).

Solo librería estándar de Python 3.9+: no hay que instalar nada.

    python cartas.py                          # http://localhost:8080
    python cartas.py --fecha-fija 2026-12-25  # para demostrar la regla del 24 de diciembre
    python cartas.py --puerto 8081

Capas (de abajo hacia arriba):
    Repositorio      -> guarda las cartas en memoria
    ClienteLista     -> cliente SOAP de ListaBuenosTraviesosService
    ServicioCartas   -> las 5 reglas de negocio
    Manejador (HTTP) -> rutas REST, seguridad, errores en JSON
    MockLista        -> simulador del servicio SOAP (/mock/lista)
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Ecuador continental: UTC-5 todo el año (sin horario de verano).
ZONA_ECUADOR = timezone(timedelta(hours=-5), "America/Guayaquil")
CARPETA = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- dominio

ESTADOS = ("RECIBIDA", "APROBADA", "EN_PRODUCCION", "ENTREGADA", "RECHAZADA")
COMPORTAMIENTOS = ("BUENO", "TRAVIESO", "PENDIENTE_VERIFICACION")

# Regla 3: transiciones válidas. ENTREGADA y RECHAZADA son estados finales.
TRANSICIONES = {
    "RECIBIDA": ("APROBADA", "RECHAZADA"),
    "APROBADA": ("EN_PRODUCCION",),
    "EN_PRODUCCION": ("ENTREGADA",),
    "ENTREGADA": (),
    "RECHAZADA": (),
}


def puede_pasar(actual, destino):
    return destino in TRANSICIONES.get(actual, ())


class ApiError(Exception):
    """Error que ya sabe con qué código HTTP responder."""

    def __init__(self, status, mensaje, detalles=None):
        super().__init__(mensaje)
        self.status = status
        self.mensaje = mensaje
        self.detalles = detalles


# ---------------------------------------------------------------- validación

def validar_carta(cuerpo):
    """Valida POST/PUT. Solo toma los campos editables: id, estado, comportamiento
    y fechaRecepcion los pone el servidor y se ignoran si el cliente los envía."""
    b = cuerpo if isinstance(cuerpo, dict) else {}
    det = {}

    remitente = b.get("remitente")
    if not isinstance(remitente, str) or not remitente.strip():
        det["remitente"] = "es obligatorio"
    elif not 2 <= len(remitente.strip()) <= 60:
        det["remitente"] = "debe tener entre 2 y 60 caracteres"

    edad = b.get("edad")
    if edad is None:
        det["edad"] = "es obligatorio"
    elif not isinstance(edad, int) or isinstance(edad, bool):
        det["edad"] = "debe ser un número entero"
    elif not 1 <= edad <= 17:
        det["edad"] = "debe estar entre 1 y 17"

    ciudad = b.get("ciudad")
    if not isinstance(ciudad, str) or not ciudad.strip():
        det["ciudad"] = "es obligatorio"

    regalos = b.get("regalos")
    if regalos is None:
        det["regalos"] = "es obligatorio"
    elif not isinstance(regalos, list):
        det["regalos"] = "debe ser una lista de texto"
    elif not 1 <= len(regalos) <= 5:
        det["regalos"] = "debe tener entre 1 y 5 elementos"
    elif any(not isinstance(r, str) or not r.strip() for r in regalos):
        det["regalos"] = "no puede contener elementos vacíos o que no sean texto"
    else:
        # Duplicado = mismo texto sin distinguir mayúsculas ni espacios al inicio/fin
        normalizados = [r.strip().lower() for r in regalos]
        if len(set(normalizados)) != len(normalizados):
            det["regalos"] = "no puede tener regalos duplicados"

    if det:
        raise ApiError(400, "Datos inválidos", det)
    return {
        "remitente": remitente.strip(),
        "edad": edad,
        "ciudad": ciudad.strip(),
        "regalos": [r.strip() for r in regalos],
    }


def validar_estado(valor):
    if valor in (None, ""):
        raise ApiError(400, "Datos inválidos", {"estado": "es obligatorio"})
    if valor not in ESTADOS:
        raise ApiError(400, "Datos inválidos", {"estado": "valor no permitido; use uno de " + ", ".join(ESTADOS)})
    return valor


# ---------------------------------------------------------------- persistencia

class Repositorio:
    """Cartas en memoria (se reinicia en cada arranque). Thread-safe y devuelve copias."""

    def __init__(self):
        self._cartas = {}
        self._siguiente_id = 1
        self._candado = threading.Lock()

    def guardar(self, carta):
        with self._candado:
            if carta.get("id") is None:
                carta["id"] = self._siguiente_id
                self._siguiente_id += 1
            self._cartas[carta["id"]] = json.loads(json.dumps(carta))
            return json.loads(json.dumps(carta))

    def buscar_por_id(self, id_):
        with self._candado:
            c = self._cartas.get(id_)
            return json.loads(json.dumps(c)) if c else None

    def buscar(self, estado=None, ciudad=None):
        """Filtros opcionales: estado exacto y ciudad sin distinguir mayúsculas."""
        ciudad = ciudad.strip().lower() if ciudad else None
        with self._candado:
            return [json.loads(json.dumps(c)) for _, c in sorted(self._cartas.items())
                    if (not estado or c["estado"] == estado)
                    and (not ciudad or c["ciudad"].lower() == ciudad)]

    def eliminar(self, id_):
        with self._candado:
            self._cartas.pop(id_, None)


# ---------------------------------------------------------------- cliente SOAP

NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
NS_LISTA = "http://polonorte.beatlech/lista"


class ListaNoDisponible(Exception):
    """El servicio SOAP respondió soap:Fault, no respondió o respondió algo que no se entiende."""


def escapar_xml(texto):
    return (str(texto).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def construir_sobre(nombre, ciudad):
    return (f'<soapenv:Envelope xmlns:soapenv="{NS_SOAP}" xmlns:pn="{NS_LISTA}">'
            "<soapenv:Body>"
            "<pn:ConsultarComportamientoRequest>"
            f"<pn:nombre>{escapar_xml(nombre)}</pn:nombre>"
            f"<pn:ciudad>{escapar_xml(ciudad)}</pn:ciudad>"
            "</pn:ConsultarComportamientoRequest>"
            "</soapenv:Body>"
            "</soapenv:Envelope>")


def interpretar_respuesta(xml):
    """soap:Fault -> ListaNoDisponible; pn:resultado BUENO/TRAVIESO -> texto."""
    if "<!DOCTYPE" in xml or "<!ENTITY" in xml:  # protección XXE / bomba de entidades
        raise ListaNoDisponible("Respuesta SOAP con DOCTYPE no permitida")
    try:
        raiz = ET.fromstring(xml)
    except ET.ParseError:
        raise ListaNoDisponible("Respuesta SOAP ilegible")

    fault = raiz.find(f".//{{{NS_SOAP}}}Fault")
    if fault is not None:
        # En SOAP 1.1 faultcode y faultstring van sin namespace
        codigo = (fault.findtext("faultcode") or "").strip()
        texto = (fault.findtext("faultstring") or "").strip()
        print(f"[SOAP] soap:Fault {codigo} - {texto}")
        raise ListaNoDisponible(texto or f"soap:Fault {codigo}")

    resultado = raiz.find(f".//{{{NS_LISTA}}}resultado")
    if resultado is None:
        raise ListaNoDisponible("La respuesta SOAP no trae pn:resultado")
    valor = (resultado.text or "").strip()
    if valor not in ("BUENO", "TRAVIESO"):
        raise ListaNoDisponible(f"Resultado desconocido de la lista: {valor}")
    return valor


class ClienteLista:
    """Cliente de ListaBuenosTraviesosService, operación ConsultarComportamiento (SOAP 1.1)."""

    def __init__(self, url, timeout=3.0):
        self.url = url
        self.timeout = timeout

    def consultar_comportamiento(self, nombre, ciudad):
        peticion = urllib.request.Request(
            self.url,
            data=construir_sobre(nombre, ciudad).encode("utf-8"),
            headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '"ConsultarComportamiento"'},
            method="POST",
        )
        try:
            with urllib.request.urlopen(peticion, timeout=self.timeout) as resp:
                cuerpo = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            # Un soap:Fault llega normalmente con HTTP 500: se lee el cuerpo igual.
            cuerpo = e.read().decode("utf-8", errors="replace")
        except Exception as e:  # conexión rechazada, timeout, DNS...
            print(f"[SOAP] No se pudo contactar la lista en {self.url}: {e}")
            raise ListaNoDisponible("La Lista de Buenos y Traviesos no responde")
        return interpretar_respuesta(cuerpo)


# ---------------------------------------------------------------- reglas de negocio

def despues_de_nochebuena(dia):
    """Del 25 al 31 de diciembre no se aceptan cartas; desde el 1 de enero sí (siguiente Navidad)."""
    return dia.month == 12 and dia.day > 24


class Reloj:
    """Fecha del negocio en hora de Ecuador, o una fecha fija para demostrar la regla 1."""

    def __init__(self, fecha_fija=None):
        self.fecha_fija = date.fromisoformat(fecha_fija) if fecha_fija else None

    def hoy(self):
        return self.fecha_fija or datetime.now(ZONA_ECUADOR).date()

    def ahora_iso(self):
        if self.fecha_fija:
            return datetime(self.fecha_fija.year, self.fecha_fija.month, self.fecha_fija.day, 12,
                            tzinfo=ZONA_ECUADOR).isoformat()
        return datetime.now(ZONA_ECUADOR).isoformat(timespec="seconds")


class ServicioCartas:
    def __init__(self, repo, lista, reloj):
        self.repo = repo
        self.lista = lista
        self.reloj = reloj

    def obtener(self, id_):
        carta = self.repo.buscar_por_id(id_)
        if carta is None:
            raise ApiError(404, f"No existe la carta {id_}")
        return carta

    def registrar(self, datos):
        """Reglas 1 y 2: fuera de plazo -> 422; toda carta nueva nace RECIBIDA."""
        hoy = self.reloj.hoy()
        if despues_de_nochebuena(hoy):
            raise ApiError(422, f"No se aceptan cartas después del 24 de diciembre (fecha actual: {hoy})")
        return self.repo.guardar({
            "id": None,
            **datos,
            "estado": "RECIBIDA",
            "comportamiento": "PENDIENTE_VERIFICACION",
            "fechaRecepcion": self.reloj.ahora_iso(),
        })

    def listar(self, estado=None, ciudad=None):
        return self.repo.buscar(estado, ciudad)

    def editar(self, id_, datos):
        """PUT: solo en RECIBIDA (regla 5 incluida: ENTREGADA nunca se modifica)."""
        carta = self.obtener(id_)
        if carta["estado"] == "ENTREGADA":
            raise ApiError(409, "Una carta ENTREGADA no se puede modificar")
        if carta["estado"] != "RECIBIDA":
            raise ApiError(409, f"Solo se pueden editar cartas en estado RECIBIDA (estado actual: {carta['estado']})")
        carta.update(datos)
        return self.repo.guardar(carta)

    def eliminar(self, id_):
        """Regla 5: ENTREGADA no se elimina."""
        carta = self.obtener(id_)
        if carta["estado"] == "ENTREGADA":
            raise ApiError(409, "Una carta ENTREGADA no se puede eliminar")
        self.repo.eliminar(id_)

    def cambiar_estado(self, id_, destino):
        """Reglas 3 y 4.
        - Transición fuera del mapa -> 409.
        - Hacia APROBADA se consulta la lista SOAP: BUENO -> APROBADA; TRAVIESO -> RECHAZADA.
        - Si la lista falla (soap:Fault / sin respuesta): la carta sigue RECIBIDA,
          comportamiento = PENDIENTE_VERIFICACION, y se responde 503 para reintentar después.
        """
        carta = self.obtener(id_)
        actual = carta["estado"]
        if not puede_pasar(actual, destino):
            permitidos = ", ".join(TRANSICIONES[actual]) or "ninguna (estado final)"
            raise ApiError(409, f"Transición no permitida: {actual} → {destino}. "
                                f"Desde {actual} solo se permite: {permitidos}")

        if destino == "APROBADA":
            try:
                comportamiento = self.lista.consultar_comportamiento(carta["remitente"], carta["ciudad"])
            except ListaNoDisponible as e:
                carta["comportamiento"] = "PENDIENTE_VERIFICACION"
                self.repo.guardar(carta)
                raise ApiError(503, f"No se pudo verificar el comportamiento: {e}. "
                                    f"La carta sigue en {actual} (PENDIENTE_VERIFICACION); reintente más tarde.")
            carta["comportamiento"] = comportamiento
            carta["estado"] = "RECHAZADA" if comportamiento == "TRAVIESO" else "APROBADA"
            print(f"[NEGOCIO] Carta {id_} de {carta['remitente']}: lista dice {comportamiento} -> {carta['estado']}")
        else:
            carta["estado"] = destino
        return self.repo.guardar(carta)


# ---------------------------------------------------------------- seguridad (HTTP Basic)

ROL_ELFO_JEFE = "ELFO_JEFE"


def _hash(clave, sal):
    return hashlib.pbkdf2_hmac("sha256", clave.encode("utf-8"), sal, 100_000)


def _usuario(clave, roles):
    sal = os.urandom(16)
    return {"sal": sal, "hash": _hash(clave, sal), "roles": roles}


# Usuarios de demo. Las claves se guardan como hash con sal, nunca en texto plano.
USUARIOS = {
    "elfojefe": _usuario("polonorte", [ROL_ELFO_JEFE]),
    "elfo": _usuario("elfo1234", ["ELFO"]),
}


def autenticar(cabecera):
    """Devuelve el usuario si la cabecera Authorization: Basic es válida, o None."""
    if not cabecera or not cabecera.startswith("Basic "):
        return None
    try:
        nombre, _, clave = base64.b64decode(cabecera[6:]).decode("utf-8").partition(":")
    except Exception:
        return None
    u = USUARIOS.get(nombre)
    if u and hmac.compare_digest(_hash(clave, u["sal"]), u["hash"]):
        return u
    return None


# ---------------------------------------------------------------- simulador SOAP

class MockLista:
    """Imita ListaBuenosTraviesosService. En producción se apunta --soap-url al servicio real.
    - Nombres de 'traviesos' -> TRAVIESO; el resto -> BUENO.
    - En mantenimiento -> HTTP 500 con soap:Fault (faultcode soap:Server, "Lista en mantenimiento")."""

    def __init__(self, traviesos=("Bart", "Dennis", "Grinch"), mantenimiento=False):
        self.traviesos = {t.strip().lower() for t in traviesos if t.strip()}
        self.mantenimiento = mantenimiento

    @staticmethod
    def fault(mensaje):
        return (f'<soap:Envelope xmlns:soap="{NS_SOAP}"><soap:Body><soap:Fault>'
                "<faultcode>soap:Server</faultcode>"
                f"<faultstring>{escapar_xml(mensaje)}</faultstring>"
                "</soap:Fault></soap:Body></soap:Envelope>")

    def responder(self, sobre):
        """Devuelve (status HTTP, xml)."""
        if self.mantenimiento:
            return 500, self.fault("Lista en mantenimiento")
        try:
            nombre = ET.fromstring(sobre).find(f".//{{{NS_LISTA}}}nombre")
        except ET.ParseError:
            nombre = None
        if nombre is None or not (nombre.text or "").strip():
            return 500, self.fault("Solicitud sin nombre")
        resultado = "TRAVIESO" if nombre.text.strip().lower() in self.traviesos else "BUENO"
        return 200, (f'<soapenv:Envelope xmlns:soapenv="{NS_SOAP}" xmlns:pn="{NS_LISTA}">'
                     "<soapenv:Body><pn:ConsultarComportamientoResponse>"
                     f"<pn:resultado>{resultado}</pn:resultado>"
                     "</pn:ConsultarComportamientoResponse></soapenv:Body></soapenv:Envelope>")


# ---------------------------------------------------------------- capa HTTP

FRASES = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
          405: "Method Not Allowed", 409: "Conflict", 422: "Unprocessable Entity",
          500: "Internal Server Error", 503: "Service Unavailable"}

SWAGGER_HTML = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<title>Taller de Santa · API de Cartas</title>
<link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css"></head>
<body><div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({url: '/v3/api-docs', dom_id: '#swagger-ui', operationsSorter: 'method'});</script>
</body></html>"""

RUTA_LISTA = re.compile(r"^/api/cartas/?$")
RUTA_CARTA = re.compile(r"^/api/cartas/([^/]+)/?$")
RUTA_ESTADO = re.compile(r"^/api/cartas/([^/]+)/estado/?$")


class Manejador(BaseHTTPRequestHandler):
    server_version = "TallerSanta/1.0"
    # servicio y mock los inyecta crear_servidor() en la subclase

    # --- utilidades de respuesta
    def _enviar(self, status, cuerpo=None, tipo="application/json; charset=utf-8", cabeceras=None):
        datos = b""
        if cuerpo is not None:
            datos = cuerpo.encode("utf-8") if isinstance(cuerpo, str) else \
                json.dumps(cuerpo, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if cuerpo is not None:
            self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        for k, v in (cabeceras or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if datos and self.command != "HEAD":
            self.wfile.write(datos)

    def _error(self, status, mensaje, detalles=None, cabeceras=None):
        cuerpo = {
            "timestamp": datetime.now(ZONA_ECUADOR).isoformat(timespec="seconds"),
            "status": status,
            "error": FRASES.get(status, "Error"),
            "mensaje": mensaje,
            "ruta": urlparse(self.path).path,
        }
        if detalles:
            cuerpo["detalles"] = detalles
        self._enviar(status, cuerpo, cabeceras=cabeceras)

    def _leer_cuerpo(self):
        largo = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(largo).decode("utf-8") if largo else ""

    def _leer_json(self):
        texto = self._leer_cuerpo()
        try:
            return json.loads(texto) if texto else None
        except json.JSONDecodeError:
            raise ApiError(400, "Cuerpo JSON inválido")

    def _requiere_rol(self, rol):
        """401 si no hay credenciales válidas, 403 si falta el rol."""
        u = autenticar(self.headers.get("Authorization"))
        if u is None:
            raise ApiError(401, "Se requiere autenticación (HTTP Basic)")
        if rol not in u["roles"]:
            raise ApiError(403, f"Se requiere el rol {rol}")

    @staticmethod
    def _id(texto):
        if not texto.isdigit():
            raise ApiError(400, f"Valor inválido para 'id': {texto}")
        return int(texto)

    def log_message(self, formato, *args):
        print(f"[HTTP] {self.address_string()} {formato % args}")

    # --- enrutador
    def _despachar(self):
        url = urlparse(self.path)
        ruta, metodo = url.path, self.command
        try:
            # Simulador SOAP
            if ruta == "/mock/lista" and metodo == "POST":
                status, xml = self.mock.responder(self._leer_cuerpo())
                return self._enviar(status, xml, tipo="text/xml; charset=utf-8")
            if ruta == "/mock/lista/mantenimiento" and metodo == "POST":
                self.mock.mantenimiento = parse_qs(url.query).get("activo", ["false"])[0] == "true"
                return self._enviar(200, {"mantenimiento": self.mock.mantenimiento})

            # Documentación
            if ruta in ("/", "/swagger-ui.html", "/docs") and metodo == "GET":
                return self._enviar(200, SWAGGER_HTML, tipo="text/html; charset=utf-8")
            if ruta == "/v3/api-docs" and metodo == "GET":
                with open(os.path.join(CARPETA, "docs", "openapi.json"), encoding="utf-8") as f:
                    return self._enviar(200, f.read())

            # API REST
            if RUTA_LISTA.match(ruta):
                if metodo == "POST":    # pública: 201 + Location; 400; 422
                    carta = self.servicio.registrar(validar_carta(self._leer_json()))
                    host = self.headers.get("Host", "localhost")
                    return self._enviar(201, carta, cabeceras={"Location": f"http://{host}/api/cartas/{carta['id']}"})
                if metodo == "GET":     # pública: filtros ?estado= y ?ciudad=
                    q = parse_qs(url.query)
                    estado = q.get("estado", [None])[0]
                    estado = validar_estado(estado) if estado else None
                    return self._enviar(200, self.servicio.listar(estado, q.get("ciudad", [None])[0]))
                raise ApiError(405, f"Método {metodo} no permitido")

            m = RUTA_ESTADO.match(ruta)
            if m:
                if metodo == "PATCH":   # rol ELFO_JEFE: 409 transición inválida; 503 si la lista falla
                    self._requiere_rol(ROL_ELFO_JEFE)
                    id_ = self._id(m.group(1))
                    cuerpo = self._leer_json()
                    destino = validar_estado(cuerpo.get("estado") if isinstance(cuerpo, dict) else None)
                    return self._enviar(200, self.servicio.cambiar_estado(id_, destino))
                raise ApiError(405, f"Método {metodo} no permitido")

            m = RUTA_CARTA.match(ruta)
            if m:
                if metodo == "GET":     # pública: 404 si no existe
                    return self._enviar(200, self.servicio.obtener(self._id(m.group(1))))
                if metodo == "PUT":     # pública: solo en RECIBIDA
                    id_ = self._id(m.group(1))
                    return self._enviar(200, self.servicio.editar(id_, validar_carta(self._leer_json())))
                if metodo == "DELETE":  # rol ELFO_JEFE: 204; 409 si ENTREGADA
                    self._requiere_rol(ROL_ELFO_JEFE)
                    self.servicio.eliminar(self._id(m.group(1)))
                    return self._enviar(204)
                raise ApiError(405, f"Método {metodo} no permitido")

            raise ApiError(404, f"No existe la ruta {metodo} {ruta}")

        except ApiError as e:
            extra = {"WWW-Authenticate": 'Basic realm="Taller de Santa"'} if e.status == 401 else None
            self._error(e.status, e.mensaje, e.detalles, extra)
        except Exception as e:  # nunca devolver una traza al cliente
            print(f"[ERROR] {ruta}: {e!r}")
            self._error(500, "Error interno")

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _despachar


def crear_servidor(puerto=8080, fecha_fija=None, soap_url=None, soap_timeout=3.0,
                   traviesos=("Bart", "Dennis", "Grinch"), host="127.0.0.1"):
    """Arma todas las piezas. Con soap_url=None usa el simulador incluido en este mismo servidor.
    Es multihilo: mientras atiende el PATCH puede responder la consulta SOAP a sí mismo."""
    servidor = ThreadingHTTPServer((host, puerto), Manejador)
    servidor.daemon_threads = True
    url = soap_url or f"http://127.0.0.1:{servidor.server_port}/mock/lista"
    servicio = ServicioCartas(Repositorio(), ClienteLista(url, soap_timeout), Reloj(fecha_fija))
    mock = MockLista(traviesos)
    # Cada servidor tiene su propia subclase con sus dependencias (permite varios en los tests)
    servidor.RequestHandlerClass = type("ManejadorCartas", (Manejador,), {"servicio": servicio, "mock": mock})
    servidor.soap_url = url
    servidor.mock = mock
    return servidor


def main():
    p = argparse.ArgumentParser(description="Taller de Santa · API de Cartas")
    p.add_argument("--puerto", type=int, default=int(os.environ.get("PUERTO", 8080)))
    p.add_argument("--fecha-fija", default=os.environ.get("FECHA_FIJA"),
                   help="AAAA-MM-DD; simula la fecha de hoy (ej. 2026-12-25 para la regla 1)")
    p.add_argument("--soap-url", default=os.environ.get("SOAP_LISTA_URL"),
                   help="URL del servicio SOAP real (por defecto, el simulador /mock/lista)")
    a = p.parse_args()

    servidor = crear_servidor(a.puerto, a.fecha_fija, a.soap_url)
    print(f"Taller de Santa escuchando en http://localhost:{a.puerto}")
    print(f"  API:     http://localhost:{a.puerto}/api/cartas")
    print(f"  Swagger: http://localhost:{a.puerto}/swagger-ui.html")
    print(f"  SOAP:    {servidor.soap_url}")
    if a.fecha_fija:
        print(f"  Fecha fija: {a.fecha_fija}")
    print("Ctrl+C para detener")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nDetenido")
        servidor.server_close()


if __name__ == "__main__":
    main()
