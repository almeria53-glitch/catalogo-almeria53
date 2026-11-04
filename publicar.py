#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Publicar apps en la Store Almería 53.

Se usa desde Termux, dentro de la carpeta del catálogo:
    cd ~/catalogo-almeria53
    python publicar.py

Con esto puedes:
  1) Publicar una app nueva o subir una versión nueva
  2) Cambiar la descripción, el icono o las capturas de una app ya publicada
  3) Quitar una app de la tienda
"""
import datetime
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

AQUI = os.path.dirname(os.path.abspath(__file__))
CATALOGO = os.path.join(AQUI, "catalogo.json")
RAW = "https://raw.githubusercontent.com/almeria53-glitch/catalogo-almeria53/main"
HOME = os.path.expanduser("~")

# Dónde busca los APK y las imágenes (se pueden cambiar con variables de entorno).
DESCARGAS = os.environ.get("DESCARGAS", os.path.join(HOME, "storage", "downloads"))
CARPETAS_IMAGEN = [
    DESCARGAS,
    os.environ.get("IMAGENES", os.path.join(HOME, "storage", "pictures")),
    os.environ.get("CAMARA", os.path.join(HOME, "storage", "dcim", "Camera")),
    os.environ.get("CAPTURAS", os.path.join(HOME, "storage", "dcim", "Screenshots")),
]


# ---------------------------------------------------------------- utilidades
def salir(msg):
    print("\n" + msg)
    sys.exit(1)


def git(*args, mostrar=True):
    r = subprocess.run(["git", "-C", AQUI] + list(args), capture_output=not mostrar, text=True)
    return r


def comprobar_carpeta():
    """Seguridad: solo trabaja si esta carpeta es de verdad el repositorio del catálogo."""
    r = subprocess.run(["git", "-C", AQUI, "remote", "get-url", "origin"], capture_output=True, text=True)
    if r.returncode != 0 or "catalogo-almeria53" not in r.stdout:
        salir("Esta no es la carpeta del catálogo (o no está conectada a GitHub).\n"
              "Entra con:  cd ~/catalogo-almeria53   y vuelve a ejecutar:  python publicar.py")


def cargar():
    with open(CATALOGO, encoding="utf-8") as f:
        datos = json.load(f)
    datos.setdefault("apps", [])
    return datos


def guardar(datos):
    with open(CATALOGO, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
        f.write("\n")


def preguntar(texto, defecto=""):
    pista = f" [Enter = dejar «{defecto}»]" if defecto else ""
    r = input(f"{texto}{pista}: ").strip()
    return r if r else defecto


def preguntar_si_no(texto):
    return input(f"{texto} (s/n): ").strip().lower().startswith("s")


def preguntar_largo(texto, defecto=""):
    print(f"{texto}")
    if defecto:
        print("(Enter sin escribir nada = dejar la que hay)")
    print("Escribe el texto. Puedes poner varias líneas; para terminar, una línea vacía:")
    lineas = []
    while True:
        try:
            linea = input()
        except EOFError:
            break
        if linea.strip() == "":
            break
        lineas.append(linea.strip())
    return " ".join(lineas) if lineas else defecto


def fecha_hora(ruta):
    return datetime.datetime.fromtimestamp(os.path.getmtime(ruta)).strftime("%d/%m %H:%M")


def listar_archivos(carpetas, extensiones, limite=15, filtro=None):
    vistos, todos = set(), []
    for c in carpetas:
        if not os.path.isdir(c):
            continue
        for nombre in os.listdir(c):
            ruta = os.path.join(c, nombre)
            if ruta in vistos or not os.path.isfile(ruta):
                continue
            if not nombre.lower().endswith(extensiones):
                continue
            if filtro and not filtro(nombre):
                continue
            vistos.add(ruta)
            todos.append(ruta)
    todos.sort(key=os.path.getmtime, reverse=True)
    return todos[:limite]


def elegir_uno(titulo, archivos, permitir_nada=None):
    print("\n" + titulo)
    for i, a in enumerate(archivos, 1):
        print(f"  {i}) {os.path.basename(a)}   ({fecha_hora(a)})")
    if permitir_nada:
        print(f"  Enter) {permitir_nada}")
    while True:
        r = input("Número: ").strip()
        if not r and permitir_nada:
            return None
        if r.isdigit() and 1 <= int(r) <= len(archivos):
            return archivos[int(r) - 1]
        print("No es un número de la lista.")


def elegir_varios(titulo, archivos):
    print("\n" + titulo)
    for i, a in enumerate(archivos, 1):
        print(f"  {i}) {os.path.basename(a)}   ({fecha_hora(a)})")
    print("  Escribe los números separados por espacios (ejemplo: 1 3 4).")
    print("  Enter) dejar como está     0) quitar todas")
    while True:
        r = input("Números: ").strip()
        if not r:
            return None
        if r == "0":
            return []
        try:
            nums = [int(x) for x in r.replace(",", " ").split()]
        except ValueError:
            print("Escribe solo números.")
            continue
        if all(1 <= n <= len(archivos) for n in nums):
            return [archivos[n - 1] for n in nums]
        print("Hay algún número que no está en la lista.")


# ---------------------------------------------------------------- APK
def leer_apk(ruta):
    """Lee paquete, versión y nombre del APK con aapt. Devuelve None si no puede."""
    try:
        r = subprocess.run(["aapt", "dump", "badging", ruta], capture_output=True, text=True)
    except FileNotFoundError:
        return "sin_aapt"
    if r.returncode != 0:
        return None
    info = {}
    for linea in r.stdout.splitlines():
        if linea.startswith("package:"):
            for clave in ("name", "versionCode", "versionName"):
                m = None
                marca = clave + "='"
                if marca in linea:
                    ini = linea.index(marca) + len(marca)
                    m = linea[ini:linea.index("'", ini)]
                if m is not None:
                    info[clave] = m
        elif linea.startswith("application-label:") and "label" not in info:
            info["label"] = linea.split(":", 1)[1].strip().strip("'")
        elif linea.startswith("application-label-es:"):
            info["label"] = linea.split(":", 1)[1].strip().strip("'")
    if "name" not in info or "versionCode" not in info:
        return None
    return info


def sacar_apk_de_zip(ruta_zip, destino_dir):
    with zipfile.ZipFile(ruta_zip) as z:
        apks = [n for n in z.namelist() if n.lower().endswith(".apk")]
        if not apks:
            return None
        z.extract(apks[0], destino_dir)
        return os.path.join(destino_dir, apks[0])


def reducir_imagen(origen, destino, maximo):
    """Copia la imagen; si está Pillow la reduce para que pese poco."""
    try:
        from PIL import Image  # opcional
        with Image.open(origen) as im:
            im.thumbnail((maximo, maximo))
            if destino.lower().endswith(".png"):
                im.save(destino, "PNG", optimize=True)
            else:
                im.convert("RGB").save(destino, "JPEG", quality=85, optimize=True)
        return
    except Exception:
        shutil.copyfile(origen, destino)


def extension(ruta):
    e = os.path.splitext(ruta)[1].lower()
    return ".png" if e == ".png" else ".jpg"


# ---------------------------------------------------------------- acciones
def datos_de_texto_e_imagenes(entrada, paquete):
    """Pide nombre, descripciones, icono y capturas. Los Enter dejan lo que ya había."""
    entrada["nombre"] = preguntar("\nNombre de la app", entrada.get("nombre", ""))
    entrada["corta"] = preguntar("Descripción corta (una frase)", entrada.get("corta", ""))
    entrada["descripcion"] = preguntar_largo("\nDescripción larga (lo que sale en la ficha de la app).",
                                            entrada.get("descripcion", ""))

    carpeta_img = os.path.join(AQUI, "imagenes", paquete)
    imagenes = listar_archivos(CARPETAS_IMAGEN, (".png", ".jpg", ".jpeg", ".webp"), limite=20)

    if imagenes:
        icono = elegir_uno("ICONO de la app (mejor una imagen cuadrada). Elige una:", imagenes,
                           permitir_nada="dejar el icono como está")
        if icono:
            os.makedirs(carpeta_img, exist_ok=True)
            for viejo in glob.glob(os.path.join(carpeta_img, "icono.*")):
                os.remove(viejo)
            nombre = "icono" + extension(icono)
            reducir_imagen(icono, os.path.join(carpeta_img, nombre), 512)
            entrada["icono"] = f"{RAW}/imagenes/{paquete}/{nombre}"

        caps = elegir_varios("CAPTURAS de la app (las que quieras, en orden):", imagenes)
        if caps is not None:
            os.makedirs(carpeta_img, exist_ok=True)
            for viejo in glob.glob(os.path.join(carpeta_img, "captura*")):
                os.remove(viejo)
            urls = []
            for i, c in enumerate(caps, 1):
                nombre = f"captura{i}{extension(c)}"
                reducir_imagen(c, os.path.join(carpeta_img, nombre), 1280)
                urls.append(f"{RAW}/imagenes/{paquete}/{nombre}")
            entrada["capturas"] = urls
    else:
        print("\n(No encuentro imágenes en Descargas ni en Fotos. Haz capturas de la app, "
              "y vuelve a elegir «Cambiar descripción o imágenes».)")


def publicar_app(datos):
    apks = listar_archivos([DESCARGAS], (".apk", ".zip"), limite=15,
                           filtro=lambda n: n.lower().endswith(".apk") or "apk" in n.lower())
    if not apks:
        salir(f"No encuentro ningún APK en {DESCARGAS}.\n"
              "Descarga el APK desde GitHub (Actions > Artifacts) y vuelve a probar.")
    elegido = elegir_uno("¿Qué archivo quieres publicar? (el más nuevo sale primero)", apks)

    temp = tempfile.mkdtemp()
    ruta_apk = elegido
    if elegido.lower().endswith(".zip"):
        ruta_apk = sacar_apk_de_zip(elegido, temp)
        if not ruta_apk:
            salir("Ese zip no contiene ningún APK.")

    info = leer_apk(ruta_apk)
    if info == "sin_aapt":
        print("\nPara leer el APK solo, instala esta herramienta (una sola vez):  pkg install aapt")
        info = None
    if info is None:
        print("No he podido leer los datos del APK. Escríbelos a mano:")
        info = {
            "name": preguntar("Nombre del paquete (ejemplo: com.almeria53.pdffacil53)"),
            "versionName": preguntar("Versión (ejemplo: 1.1)"),
            "versionCode": preguntar("Número de versión interno (ejemplo: 2)"),
        }
        if not info["name"] or not info["versionCode"].isdigit():
            salir("Faltan datos. No se ha publicado nada.")

    paquete = info["name"]
    codigo = int(info["versionCode"])
    version = info.get("versionName", str(codigo))
    print(f"\nHe leído: {paquete}  versión {version}  (interna {codigo})")

    entrada = next((a for a in datos["apps"] if a["paquete"] == paquete), None)
    nueva = entrada is None
    if nueva:
        entrada = {"paquete": paquete, "nombre": info.get("label", ""), "corta": "", "descripcion": "",
                   "version": version, "codigo": codigo, "apk": "", "icono": "", "capturas": [],
                   "actualizado": "", "tamanoMb": 0}
        print("Es una app NUEVA en la tienda.")
    else:
        print(f"Ya está en la tienda con la versión {entrada.get('version')} (interna {entrada.get('codigo')}).")
        if codigo <= int(entrada.get("codigo", 0)):
            print("OJO: esta versión no es más nueva que la publicada. "
                  "Los móviles no la verían como actualización.")
            if not preguntar_si_no("¿Publicarla igualmente?"):
                salir("No se ha publicado nada.")

    datos_de_texto_e_imagenes(entrada, paquete)

    os.makedirs(os.path.join(AQUI, "apks"), exist_ok=True)
    destino = os.path.join(AQUI, "apks", paquete + ".apk")
    shutil.copyfile(ruta_apk, destino)
    entrada["apk"] = f"{RAW}/apks/{paquete}.apk"
    entrada["version"] = version
    entrada["codigo"] = codigo
    entrada["tamanoMb"] = round(os.path.getsize(destino) / 1048576, 1)
    entrada["actualizado"] = datetime.date.today().isoformat()

    if nueva:
        datos["apps"].append(entrada)
    guardar(datos)
    subir(f"{'Nueva app' if nueva else 'Actualizada'}: {entrada['nombre']} {version}")
    shutil.rmtree(temp, ignore_errors=True)


def elegir_app(datos, titulo):
    if not datos["apps"]:
        salir("La tienda todavía no tiene apps.")
    print("\n" + titulo)
    for i, a in enumerate(datos["apps"], 1):
        print(f"  {i}) {a['nombre']}  (versión {a.get('version', '?')})")
    while True:
        r = input("Número: ").strip()
        if r.isdigit() and 1 <= int(r) <= len(datos["apps"]):
            return datos["apps"][int(r) - 1]
        print("No es un número de la lista.")


def cambiar_textos(datos):
    entrada = elegir_app(datos, "¿De qué app quieres cambiar la descripción o las imágenes?")
    datos_de_texto_e_imagenes(entrada, entrada["paquete"])
    entrada["actualizado"] = entrada.get("actualizado") or datetime.date.today().isoformat()
    guardar(datos)
    subir(f"Cambios en la ficha de {entrada['nombre']}")


def quitar_app(datos):
    entrada = elegir_app(datos, "¿Qué app quieres QUITAR de la tienda?")
    if not preguntar_si_no(f"¿Seguro que quitas «{entrada['nombre']}»?"):
        salir("No se ha quitado nada.")
    datos["apps"] = [a for a in datos["apps"] if a["paquete"] != entrada["paquete"]]
    for ruta in (os.path.join(AQUI, "apks", entrada["paquete"] + ".apk"),):
        if os.path.exists(ruta):
            os.remove(ruta)
    shutil.rmtree(os.path.join(AQUI, "imagenes", entrada["paquete"]), ignore_errors=True)
    guardar(datos)
    subir(f"Quitada: {entrada['nombre']}")


def ver_apps(datos):
    if not datos["apps"]:
        print("\nLa tienda todavía no tiene apps.")
    for a in datos["apps"]:
        print(f"\n• {a['nombre']}  v{a.get('version')}  ({a.get('tamanoMb', 0)} MB)")
        print(f"  {a.get('corta', '')}")
        print(f"  icono: {'sí' if a.get('icono') else 'no'}   capturas: {len(a.get('capturas', []))}")


def subir(mensaje):
    print("\nSubiendo a GitHub…")
    git("add", "-A")
    r = git("commit", "-m", mensaje)
    if r.returncode != 0:
        print("(No había cambios nuevos que subir.)")
    r = git("push")
    if r.returncode == 0:
        print("\nListo. Abre la tienda en el móvil y toca «Actualizar lista» (flechas arriba a la derecha).")
    else:
        print("\nNo se pudo subir. Mira el mensaje de arriba (puede ser la conexión o la clave de GitHub) "
              "y vuelve a ejecutar:  git push")


def main():
    comprobar_carpeta()
    datos = cargar()
    while True:
        print("\n=== Store Almería 53 · Publicar ===")
        print("  1) Publicar una app nueva o una versión nueva")
        print("  2) Cambiar descripción o imágenes de una app")
        print("  3) Quitar una app de la tienda")
        print("  4) Ver las apps publicadas")
        print("  0) Salir")
        op = input("Elige: ").strip()
        if op == "1":
            publicar_app(datos)
        elif op == "2":
            cambiar_textos(datos)
        elif op == "3":
            quitar_app(datos)
        elif op == "4":
            ver_apps(datos)
        elif op == "0":
            break
        else:
            print("Escribe 1, 2, 3, 4 o 0.")
        datos = cargar()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelado.")
