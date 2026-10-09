#!/usr/bin/env python3
"""Chat de terminal para computadoras en una misma red local (LAN).

Sin dependencias externas: funciona con Python 3.9+ en Linux y Windows.
"""

import argparse
import datetime
import json
import socket
import socketserver
import threading

PUERTO_POR_DEFECTO = 5050
MAX_PAQUETE = 131072  # bytes por paquete JSON
MAX_MENSAJE = 20000  # caracteres por mensaje completo
MAX_LINEAS = 500
MAX_NOMBRE = 24


def texto_seguro(valor, limite):
    """Evita caracteres de control que puedan alterar una terminal."""
    if not isinstance(valor, str):
        return ""
    return "".join(c for c in valor if c.isprintable()).strip()[:limite]


def mensaje_seguro(valor):
    """Conserva saltos e indentacion, pero elimina controles de terminal peligrosos."""
    if not isinstance(valor, str):
        return ""
    valor = valor.replace("\r\n", "\n")
    return "".join(c for c in valor if c.isprintable() or c in "\n\t").rstrip("\n")


class EditorMultilinea:
    """Acumula lineas para enviarlas como UN mensaje al usar /enviar."""
    def __init__(self):
        self.lineas = []
        self.longitud = 0
        self.excedido = False

    def agregar(self, linea):
        extra = len(linea) + (1 if self.lineas else 0)
        if len(self.lineas) >= MAX_LINEAS or self.longitud + extra > MAX_MENSAJE:
            self.excedido = True
            return False
        self.lineas.append(linea)
        self.longitud += extra
        return True

    def contenido(self):
        return "\n".join(self.lineas)


def empaquetar(datos):
    return (json.dumps(datos, ensure_ascii=False) + "\n").encode("utf-8")


def leer_paquete(archivo):
    linea = archivo.readline(MAX_PAQUETE + 1)
    if not linea:
        return None
    if len(linea) > MAX_PAQUETE or not linea.endswith(b"\n"):
        raise ValueError("Paquete demasiado largo o incompleto")
    paquete = json.loads(linea.decode("utf-8"))
    if not isinstance(paquete, dict):
        raise ValueError("Formato de mensaje incorrecto")
    return paquete


def hora():
    return datetime.datetime.now().strftime("%H:%M")


class Participante:
    def __init__(self, conexion, nombre):
        self.conexion = conexion
        self.nombre = nombre
        self.bloqueo_envio = threading.Lock()


class ServidorChat(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, direccion):
        super().__init__(direccion, ManejadorChat)
        self.participantes = {}
        self.bloqueo = threading.Lock()

    def agregar(self, conexion, nombre_solicitado):
        with self.bloqueo:
            usados = {p.nombre.casefold() for p in self.participantes.values()}
            nombre = nombre_solicitado or "Invitado"
            base = nombre
            numero = 2
            while nombre.casefold() in usados:
                sufijo = f"_{numero}"
                nombre = base[:MAX_NOMBRE - len(sufijo)] + sufijo
                numero += 1
            participante = Participante(conexion, nombre)
            self.participantes[conexion] = participante
        return participante

    def eliminar(self, participante):
        with self.bloqueo:
            return self.participantes.pop(participante.conexion, None) is not None

    @staticmethod
    def enviar(participante, paquete):
        try:
            with participante.bloqueo_envio:
                participante.conexion.sendall(empaquetar(paquete))
            return True
        except OSError:
            return False

    def difundir(self, paquete):
        with self.bloqueo:
            destino = list(self.participantes.values())
        for participante in destino:
            self.enviar(participante, paquete)

    def usuarios(self):
        with self.bloqueo:
            return sorted(p.nombre for p in self.participantes.values())


class ManejadorChat(socketserver.BaseRequestHandler):
    def handle(self):
        servidor = self.server
        participante = None
        try:
            self.request.settimeout(15)  # Plazo para indicar el nombre
            with self.request.makefile("rb") as entrada:
                saludo = leer_paquete(entrada)
                if not saludo or saludo.get("tipo") != "entrar":
                    self.request.sendall(empaquetar({"tipo": "error", "texto": "Falta el saludo inicial"}))
                    return

                nombre = texto_seguro(saludo.get("nombre"), MAX_NOMBRE)
                participante = servidor.agregar(self.request, nombre)
                self.request.settimeout(None)
                servidor.enviar(participante, {
                    "tipo": "bienvenida", "nombre": participante.nombre,
                    "texto": f"Conectado al chat. Escribe /ayuda para ver los comandos."
                })
                servidor.difundir({
                    "tipo": "sistema", "texto": f"{participante.nombre} se conectó.", "hora": hora()
                })
                print(f"[+] {participante.nombre} desde {self.client_address[0]}", flush=True)

                while True:
                    paquete = leer_paquete(entrada)
                    if paquete is None:
                        break
                    tipo = paquete.get("tipo")
                    if tipo == "mensaje":
                        texto = mensaje_seguro(paquete.get("texto"))
                        if len(texto) > MAX_MENSAJE:
                            servidor.enviar(participante, {
                                "tipo": "error", "texto": f"Mensaje mayor de {MAX_MENSAJE} caracteres; no enviado."
                            })
                        elif texto.strip():
                            servidor.difundir({
                                "tipo": "mensaje", "nombre": participante.nombre,
                                "texto": texto, "hora": hora()
                            })
                    elif tipo == "usuarios":
                        servidor.enviar(participante, {
                            "tipo": "usuarios", "nombres": servidor.usuarios()
                        })
                    elif tipo == "salir":
                        break
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as error:
            if participante is not None:
                print(f"[!] Desconexión de {participante.nombre}: {error}", flush=True)
        finally:
            if participante and servidor.eliminar(participante):
                servidor.difundir({
                    "tipo": "sistema", "texto": f"{participante.nombre} salió.", "hora": hora()
                })
                print(f"[-] {participante.nombre} desconectado", flush=True)


def iniciar_servidor(puerto):
    try:
        with ServidorChat(("0.0.0.0", puerto)) as servidor:
            print("=" * 53)
            print(" CHAT LAN - SERVIDOR")
            print(f" Puerto TCP: {puerto}")
            print(" Escuchando en todas las interfaces de red.")
            print(" En Linux, consulta tu IP con: hostname -I")
            print(" En Windows, consulta tu IP con: ipconfig")
            print(" Presiona Ctrl+C para detener el servidor.")
            print("=" * 53, flush=True)
            try:
                servidor.serve_forever(poll_interval=0.25)
            except KeyboardInterrupt:
                print("\nApagando el servidor...")
    except OSError as error:
        print(f"No se pudo iniciar el servidor: {error}")
        raise SystemExit(1)


def mostrar(paquete):
    tipo = paquete.get("tipo")
    if tipo == "bienvenida":
        print(f"\n[OK] {paquete.get('texto', '')} Tu nombre: {paquete.get('nombre', '')}")
    elif tipo == "sistema":
        print(f"\n[{paquete.get('hora', '--:--')}] * {paquete.get('texto', '')}")
    elif tipo == "mensaje":
        texto = paquete.get("texto", "")
        cabecera = f"[{paquete.get('hora', '--:--')}] {paquete.get('nombre', '?')}:"
        if "\n" in texto:
            print(f"\n{cabecera}\n{texto}\n", flush=True)
        else:
            print(f"\n{cabecera} {texto}", flush=True)
    elif tipo == "usuarios":
        nombres = paquete.get("nombres", [])
        if isinstance(nombres, list):
            print("\nUsuarios conectados: " + ", ".join(nombres))
    elif tipo == "error":
        print("\n[ERROR] " + str(paquete.get("texto", "Error desconocido")))


def iniciar_cliente(ip, puerto, nombre):
    try:
        conexion = socket.create_connection((ip, puerto), timeout=8)
    except OSError as error:
        print(f"No se pudo conectar a {ip}:{puerto}: {error}")
        print("Comprueba que el servidor esté abierto, la IP sea correcta y el firewall permita TCP.")
        raise SystemExit(1)

    detener = threading.Event()
    bloqueo_envio = threading.Lock()

    def enviar(paquete):
        with bloqueo_envio:
            conexion.sendall(empaquetar(paquete))

    def cerrar():
        if not detener.is_set():
            detener.set()
            try:
                conexion.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def escribir():
        editor = None
        while not detener.is_set():
            try:
                entrada = input(" ... " if editor is not None else " > ")
            except (EOFError, KeyboardInterrupt):
                break

            comando = entrada.strip().lower()
            try:
                if editor is not None:
                    if comando == "/cancelar":
                        editor = None
                        print("[INFO] Bloque descartado; no se envio nada.")
                    elif comando == "/enviar":
                        contenido = editor.contenido()
                        excedido = editor.excedido
                        editor = None
                        if excedido:
                            print(f"[ERROR] Bloque demasiado largo (limite {MAX_MENSAJE} caracteres / {MAX_LINEAS} lineas). No se envio; vuelve a intentarlo con un bloque mas corto.")
                        elif contenido.strip():
                            enviar({"tipo": "mensaje", "texto": contenido})
                        else:
                            print("[INFO] El bloque estaba vacio.")
                    else:
                        if not editor.agregar(entrada) and not getattr(editor, "aviso_mostrado", False):
                            print("[ERROR] Se supero el limite del bloque. Escribe /cancelar o /enviar (no se enviara incompleto).")
                            editor.aviso_mostrado = True
                    continue

                if not entrada.strip():
                    continue
                if comando == "/salir":
                    enviar({"tipo": "salir"})
                    break
                elif comando == "/usuarios":
                    enviar({"tipo": "usuarios"})
                elif comando == "/ayuda":
                    print("\nComandos: /multi (o /pegar) | /enviar | /cancelar | /usuarios | /ayuda | /salir")
                    print("En /multi puedes pegar muchas lineas: solo /enviar manda el bloque completo.")
                elif comando in ("/multi", "/pegar", "/bloque"):
                    editor = EditorMultilinea()
                    print("[MULTILINEA] Pega o escribe todo el texto. /enviar = mandar todo; /cancelar = descartar.")
                    print(f"[MULTILINEA] Limite: {MAX_MENSAJE} caracteres o {MAX_LINEAS} lineas. Se respetan los espacios y saltos de linea.")
                else:
                    contenido = mensaje_seguro(entrada)
                    if len(contenido) > MAX_MENSAJE:
                        print(f"[ERROR] Mensaje demasiado largo (maximo {MAX_MENSAJE} caracteres).")
                    elif contenido.strip():
                        enviar({"tipo": "mensaje", "texto": contenido})
            except OSError:
                print("\nSe perdio la conexion con el servidor.")
                break
        cerrar()

    try:
        conexion.settimeout(None)
        enviar({"tipo": "entrar", "nombre": texto_seguro(nombre, MAX_NOMBRE)})
        print(f"Conectando a {ip}:{puerto}...", flush=True)
        print("Comandos: /multi | /usuarios | /ayuda | /salir")
        hilo = threading.Thread(target=escribir, daemon=True)
        hilo.start()
        with conexion.makefile("rb") as entrada:
            while not detener.is_set():
                paquete = leer_paquete(entrada)
                if paquete is None:
                    break
                mostrar(paquete)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as error:
        if not detener.is_set():
            print(f"\nConexión terminada: {error}")
    except KeyboardInterrupt:
        pass
    finally:
        cerrar()
        conexion.close()
        print("\nChat finalizado.")


def puerto_valido(valor):
    try:
        numero = int(valor)
    except ValueError:
        raise argparse.ArgumentTypeError("El puerto debe ser un número")
    if not 1 <= numero <= 65535:
        raise argparse.ArgumentTypeError("El puerto debe estar entre 1 y 65535")
    return numero


def main():
    parser = argparse.ArgumentParser(description="Chat en terminal para una red local (LAN)")
    comandos = parser.add_subparsers(dest="modo", required=True)
    servidor = comandos.add_parser("servidor", help="Abrir servidor de chat")
    servidor.add_argument("--puerto", type=puerto_valido, default=PUERTO_POR_DEFECTO)
    cliente = comandos.add_parser("cliente", help="Unirse a un servidor de chat")
    cliente.add_argument("ip", help="IP de la computadora que ejecuta el servidor")
    cliente.add_argument("--nombre", default="Invitado", help="Nombre en el chat")
    cliente.add_argument("--puerto", type=puerto_valido, default=PUERTO_POR_DEFECTO)
    argumentos = parser.parse_args()
    if argumentos.modo == "servidor":
        iniciar_servidor(argumentos.puerto)
    else:
        iniciar_cliente(argumentos.ip, argumentos.puerto, argumentos.nombre)


if __name__ == "__main__":
    main()
