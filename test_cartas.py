"""
Pruebas automáticas (solo librería estándar):

    python -m unittest -v

Levantan el servidor real en un puerto libre con el simulador SOAP real (HTTP de verdad).
"""
import base64
import json
import threading
import unittest
import urllib.error
import urllib.request

import cartas

JEFE = "Basic " + base64.b64encode(b"elfojefe:polonorte").decode()
ELFO = "Basic " + base64.b64encode(b"elfo:elfo1234").decode()
CARTA_OK = {"remitente": "Valentina", "edad": 8, "ciudad": "Guayaquil", "regalos": ["Bicicleta", "Libro"]}


class ServidorDePrueba:
    def __init__(self, fecha_fija="2026-12-10"):
        self.srv = cartas.crear_servidor(puerto=0, fecha_fija=fecha_fija)
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def cerrar(self):
        self.srv.shutdown()
        self.srv.server_close()

    def pedir(self, metodo, ruta, cuerpo=None, auth=None, crudo=None):
        datos = crudo.encode() if crudo is not None else (json.dumps(cuerpo).encode() if cuerpo is not None else None)
        req = urllib.request.Request(self.base + ruta, data=datos, method=metodo)
        if datos is not None:
            req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("Authorization", auth)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status, texto, cab = r.status, r.read().decode(), r.headers
        except urllib.error.HTTPError as e:
            status, texto, cab = e.code, e.read().decode(), e.headers
        return status, (json.loads(texto) if texto else None), cab


class ApiCartasTest(unittest.TestCase):
    """Pruebas de integración de la API (fecha fija 10/dic para que la regla 1 deje registrar)."""

    @classmethod
    def setUpClass(cls):
        cls.s = ServidorDePrueba()

    @classmethod
    def tearDownClass(cls):
        cls.s.cerrar()

    def crear(self, datos=CARTA_OK):
        status, c, _ = self.s.pedir("POST", "/api/cartas", datos)
        self.assertEqual(status, 201)
        return c["id"]

    def patch_estado(self, id_, estado, auth=JEFE):
        return self.s.pedir("PATCH", f"/api/cartas/{id_}/estado", {"estado": estado}, auth)

    def test_registrar_nace_recibida_con_datos_del_servidor(self):  # regla 2
        status, c, cab = self.s.pedir("POST", "/api/cartas", {
            **CARTA_OK, "id": 999, "estado": "ENTREGADA", "comportamiento": "BUENO", "fechaRecepcion": "2000-01-01"})
        self.assertEqual(status, 201)
        self.assertTrue(cab["Location"].endswith(f"/api/cartas/{c['id']}"))
        self.assertNotEqual(c["id"], 999)
        self.assertEqual(c["estado"], "RECIBIDA")
        self.assertEqual(c["comportamiento"], "PENDIENTE_VERIFICACION")
        self.assertTrue(c["fechaRecepcion"].startswith("2026-12-10"))

    def test_validaciones_400(self):
        status, e, _ = self.s.pedir("POST", "/api/cartas", {"remitente": "A", "edad": 18, "ciudad": "", "regalos": []})
        self.assertEqual(status, 400)
        for campo in ("remitente", "edad", "ciudad", "regalos"):
            self.assertIn(campo, e["detalles"])

        self.assertEqual(self.s.pedir("POST", "/api/cartas", {**CARTA_OK, "regalos": list("abcdef")})[0], 400)
        status, e, _ = self.s.pedir("POST", "/api/cartas", {**CARTA_OK, "regalos": ["Lego", " lego "]})
        self.assertEqual(status, 400)
        self.assertEqual(e["detalles"]["regalos"], "no puede tener regalos duplicados")
        self.assertEqual(self.s.pedir("POST", "/api/cartas", {**CARTA_OK, "edad": 7.5})[0], 400)
        self.assertEqual(self.s.pedir("POST", "/api/cartas", {**CARTA_OK, "edad": True})[0], 400)
        self.assertEqual(self.s.pedir("POST", "/api/cartas", crudo="{ esto no es json")[0], 400)

    def test_listar_filtros_y_404(self):
        self.crear({"remitente": "Luis", "edad": 9, "ciudad": "Cuenca", "regalos": ["Pelota"]})
        status, lista, _ = self.s.pedir("GET", "/api/cartas?ciudad=cuenca&estado=RECIBIDA")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(lista), 1)
        self.assertTrue(all(c["ciudad"] == "Cuenca" and c["estado"] == "RECIBIDA" for c in lista))
        self.assertEqual(self.s.pedir("GET", "/api/cartas?estado=INVENTADO")[0], 400)
        self.assertEqual(self.s.pedir("GET", "/api/cartas/987654")[0], 404)
        self.assertEqual(self.s.pedir("GET", "/api/cartas/abc")[0], 400)

    def test_flujo_completo_bueno_reglas_3_y_5(self):
        id_ = self.crear()
        self.assertEqual(self.patch_estado(id_, "ENTREGADA")[0], 409)          # salto inválido
        status, c, _ = self.patch_estado(id_, "APROBADA")                     # SOAP real -> BUENO
        self.assertEqual(status, 200)
        self.assertEqual((c["estado"], c["comportamiento"]), ("APROBADA", "BUENO"))
        self.assertEqual(self.s.pedir("PUT", f"/api/cartas/{id_}", CARTA_OK)[0], 409)  # ya no RECIBIDA
        self.assertEqual(self.patch_estado(id_, "EN_PRODUCCION")[0], 200)
        status, c, _ = self.patch_estado(id_, "ENTREGADA")
        self.assertEqual(c["estado"], "ENTREGADA")
        self.assertEqual(self.s.pedir("PUT", f"/api/cartas/{id_}", CARTA_OK)[0], 409)  # regla 5
        self.assertEqual(self.s.pedir("DELETE", f"/api/cartas/{id_}", auth=JEFE)[0], 409)

    def test_travieso_pasa_a_rechazada_regla_4(self):
        id_ = self.crear({"remitente": "Bart", "edad": 10, "ciudad": "Quito", "regalos": ["Patineta"]})
        status, c, _ = self.patch_estado(id_, "APROBADA")
        self.assertEqual(status, 200)
        self.assertEqual((c["estado"], c["comportamiento"]), ("RECHAZADA", "TRAVIESO"))
        self.assertEqual(self.patch_estado(id_, "APROBADA")[0], 409)  # RECHAZADA es final

    def test_soap_fault_503_y_queda_pendiente(self):
        id_ = self.crear()
        self.s.srv.mock.mantenimiento = True
        try:
            status, e, _ = self.patch_estado(id_, "APROBADA")
            self.assertEqual(status, 503)
            self.assertIn("Lista en mantenimiento", e["mensaje"])
        finally:
            self.s.srv.mock.mantenimiento = False
        _, c, _ = self.s.pedir("GET", f"/api/cartas/{id_}")
        self.assertEqual((c["estado"], c["comportamiento"]), ("RECIBIDA", "PENDIENTE_VERIFICACION"))
        self.assertEqual(self.patch_estado(id_, "APROBADA")[1]["estado"], "APROBADA")  # reintento

    def test_editar_en_recibida(self):
        id_ = self.crear()
        status, c, _ = self.s.pedir("PUT", f"/api/cartas/{id_}",
                                    {"remitente": "Valentina R", "edad": 9, "ciudad": "Manta", "regalos": ["Patines"]})
        self.assertEqual(status, 200)
        self.assertEqual((c["id"], c["ciudad"], c["estado"]), (id_, "Manta", "RECIBIDA"))

    def test_seguridad_401_403_204(self):
        id_ = self.crear()
        status, _, cab = self.s.pedir("DELETE", f"/api/cartas/{id_}")
        self.assertEqual(status, 401)
        self.assertIn("Basic", cab["WWW-Authenticate"])
        self.assertEqual(self.s.pedir("DELETE", f"/api/cartas/{id_}", auth=ELFO)[0], 403)
        self.assertEqual(self.patch_estado(id_, "RECHAZADA", auth=None)[0], 401)
        mala = "Basic " + base64.b64encode(b"elfojefe:otra").decode()
        self.assertEqual(self.s.pedir("DELETE", f"/api/cartas/{id_}", auth=mala)[0], 401)
        self.assertEqual(self.s.pedir("DELETE", f"/api/cartas/{id_}", auth=JEFE)[0], 204)
        self.assertEqual(self.s.pedir("GET", f"/api/cartas/{id_}")[0], 404)


class FueraDePlazoTest(unittest.TestCase):
    """Regla 1: con la fecha fija en 25 de diciembre, registrar responde 422."""

    def test_despues_del_24_responde_422(self):
        s = ServidorDePrueba(fecha_fija="2026-12-25")
        try:
            status, e, _ = s.pedir("POST", "/api/cartas", CARTA_OK)
            self.assertEqual(status, 422)
            self.assertIn("24 de diciembre", e["mensaje"])
        finally:
            s.cerrar()

    def test_limites_de_fecha(self):
        from datetime import date
        self.assertFalse(cartas.despues_de_nochebuena(date(2026, 12, 24)))
        self.assertTrue(cartas.despues_de_nochebuena(date(2026, 12, 25)))
        self.assertTrue(cartas.despues_de_nochebuena(date(2026, 12, 31)))
        self.assertFalse(cartas.despues_de_nochebuena(date(2027, 1, 1)))


class SoapYEstadosTest(unittest.TestCase):
    """Pruebas unitarias del cliente SOAP y la máquina de estados (sin servidor)."""

    def test_lee_bueno_y_travieso(self):
        xml = (f'<soapenv:Envelope xmlns:soapenv="{cartas.NS_SOAP}" xmlns:pn="{cartas.NS_LISTA}"><soapenv:Body>'
               "<pn:ConsultarComportamientoResponse><pn:resultado>BUENO</pn:resultado>"
               "</pn:ConsultarComportamientoResponse></soapenv:Body></soapenv:Envelope>")
        self.assertEqual(cartas.interpretar_respuesta(xml), "BUENO")
        self.assertEqual(cartas.interpretar_respuesta(xml.replace("BUENO", "TRAVIESO")), "TRAVIESO")

    def test_fault_lanza_error_con_faultstring(self):
        xml = cartas.MockLista.fault("Lista en mantenimiento")
        with self.assertRaises(cartas.ListaNoDisponible) as ctx:
            cartas.interpretar_respuesta(xml)
        self.assertEqual(str(ctx.exception), "Lista en mantenimiento")

    def test_servicio_caido(self):
        with self.assertRaises(cartas.ListaNoDisponible):
            cartas.ClienteLista("http://127.0.0.1:1/no-existe", timeout=1).consultar_comportamiento("Ana", "Quito")

    def test_sobre_escapa_xml(self):
        sobre = cartas.construir_sobre("<Ana & Co>", "Quito")
        self.assertIn("<pn:nombre>&lt;Ana &amp; Co&gt;</pn:nombre>", sobre)
        self.assertIn(f'xmlns:pn="{cartas.NS_LISTA}"', sobre)

    def test_maquina_de_estados(self):
        self.assertTrue(cartas.puede_pasar("RECIBIDA", "APROBADA"))
        self.assertTrue(cartas.puede_pasar("RECIBIDA", "RECHAZADA"))
        self.assertTrue(cartas.puede_pasar("APROBADA", "EN_PRODUCCION"))
        self.assertTrue(cartas.puede_pasar("EN_PRODUCCION", "ENTREGADA"))
        self.assertFalse(cartas.puede_pasar("RECIBIDA", "ENTREGADA"))
        self.assertFalse(cartas.puede_pasar("APROBADA", "RECHAZADA"))
        self.assertFalse(cartas.puede_pasar("ENTREGADA", "RECIBIDA"))
        self.assertFalse(cartas.TRANSICIONES["RECHAZADA"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
