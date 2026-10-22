"""Gira sui server di GitHub (GitHub Actions), non sul PC: pubblica su Instagram i contenuti in coda all'ora giusta.

La coda la riempie strumenti/pubblica/programma.py a PC acceso: coda/<codice>/post.json + immagini o video.
A ogni giro (ogni 10 minuti circa) prende i contenuti che scadono nei prossimi 20 minuti, aspetta l'ora esatta e
li pubblica. L'esito va in fatti/<codice>.json (o errori/<codice>.json) e la cartella in coda viene tolta.
Il token di Instagram sta nel segreto IG_TOKEN e una volta a settimana si rinnova da solo.
"""
import base64
import datetime as dt
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = os.environ.get("GITHUB_REPOSITORY", "oltremappa/oltremappa-media")
RAW = f"https://raw.githubusercontent.com/{REPO}/main"
API_IG = "https://graph.instagram.com"
ANTICIPO = dt.timedelta(minutes=20)
RITARDO_MASSIMO = dt.timedelta(hours=6)
PROVA = os.environ.get("PROVA") == "1"
TOKEN = os.environ["IG_TOKEN"]


def ig(percorso, parametri=None, metodo="GET"):
    dati = urllib.parse.urlencode(dict(parametri or {}, access_token=TOKEN))
    richiesta = (urllib.request.Request(f"{API_IG}/{percorso}?{dati}") if metodo == "GET" else
                 urllib.request.Request(f"{API_IG}/{percorso}", data=dati.encode(), method=metodo))
    try:
        with urllib.request.urlopen(richiesta, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            errore = json.load(e).get("error", {})
        except ValueError:
            errore = {}
        raise RuntimeError(f"Errore Instagram {e.code}: {errore.get('message', 'sconosciuto')}") from None


def aspetta(id_contenitore, minuti=10):
    fine = time.time() + minuti * 60
    while time.time() < fine:
        stato = ig(id_contenitore, {"fields": "status_code,status"})
        if stato.get("status_code") == "FINISHED":
            return
        if stato.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram ha rifiutato il file: {stato.get('status')}")
        time.sleep(10)
    raise RuntimeError("Instagram non ha finito di elaborare il file in tempo")


def contenitore(uid, post):
    """Prepara su Instagram il contenuto e restituisce gli id da pubblicare (le storie ne hanno uno per schermata)."""
    url = lambda nome: f"{RAW}/coda/{urllib.parse.quote(post['codice'])}/{nome}"
    if post["tipo"] == "storie":
        schermate = [ig(f"{uid}/media", {"media_type": "STORIES", "image_url": url(nome)}, metodo="POST")["id"]
                     for nome in post["schermate"]]
        for s in schermate:
            aspetta(s)
        return schermate
    if post["tipo"] == "reel":
        c = ig(f"{uid}/media", {"media_type": "REELS", "video_url": url(post["video"]), "cover_url": url(post["copertina"]),
                                "caption": post["caption"], "share_to_feed": "true"}, metodo="POST")["id"]
        aspetta(c, minuti=20)
        return [c]
    figli = []
    for nome in post["slide"]:
        parametri = {"image_url": url(nome), "is_carousel_item": "true"}
        alt = post.get("alt", {}).get(nome)
        try:
            figli.append(ig(f"{uid}/media", dict(parametri, alt_text=alt) if alt else parametri, metodo="POST")["id"])
        except RuntimeError as e:
            if not alt or "alt" not in str(e).lower():
                raise
            figli.append(ig(f"{uid}/media", parametri, metodo="POST")["id"])
    for f in figli:
        aspetta(f)
    c = ig(f"{uid}/media", {"media_type": "CAROUSEL", "children": ",".join(figli), "caption": post["caption"]},
           metodo="POST")["id"]
    aspetta(c)
    return [c]


def salva(cartella, codice, dati):
    os.makedirs(cartella, exist_ok=True)
    with open(os.path.join(cartella, f"{codice}.json"), "w", encoding="utf-8") as f:
        json.dump(dati, f, ensure_ascii=False, indent=1)


def rinnova_token():
    """Una volta a settimana: nuovo token Instagram, salvato nel segreto IG_TOKEN."""
    file_stato = "stato/token.json"
    try:
        with open(file_stato, encoding="utf-8") as f:
            ultimo = dt.datetime.fromisoformat(json.load(f)["rinnovato"])
    except (FileNotFoundError, KeyError, ValueError):
        ultimo = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    adesso = dt.datetime.now(dt.timezone.utc)
    if adesso - ultimo < dt.timedelta(days=7) or not os.environ.get("GH_PAT"):
        return
    nuovo = ig("refresh_access_token", {"grant_type": "ig_refresh_token"})["access_token"]
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pynacl"], check=True)
    from nacl import encoding, public
    intest = {"Authorization": f"Bearer {os.environ['GH_PAT']}", "Accept": "application/vnd.github+json",
              "User-Agent": "oltremappa"}
    with urllib.request.urlopen(urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/actions/secrets/public-key", headers=intest)) as r:
        chiave = json.load(r)
    cifrato = public.SealedBox(public.PublicKey(chiave["key"].encode(), encoding.Base64Encoder())).encrypt(nuovo.encode())
    corpo = json.dumps({"encrypted_value": base64.b64encode(cifrato).decode(), "key_id": chiave["key_id"]}).encode()
    urllib.request.urlopen(urllib.request.Request(f"https://api.github.com/repos/{REPO}/actions/secrets/IG_TOKEN",
                                                  data=corpo, method="PUT", headers=intest)).close()
    os.makedirs("stato", exist_ok=True)
    with open(file_stato, "w", encoding="utf-8") as f:
        json.dump({"rinnovato": adesso.isoformat()}, f)
    print("Token Instagram rinnovato.")


def main():
    adesso = dt.datetime.now(dt.timezone.utc)
    uid = ig("me", {"fields": "user_id"})["user_id"]
    coda = []
    for file in glob.glob("coda/*/post.json"):
        with open(file, encoding="utf-8") as f:
            post = json.load(f)
        quando = dt.datetime.fromisoformat(post["quando"])
        if PROVA or quando - adesso <= ANTICIPO:
            coda.append((quando, post, os.path.dirname(file)))
    for quando, post, cartella in sorted(coda, key=lambda x: x[0]):
        codice = post["codice"]
        if PROVA:
            try:
                contenitore(uid, post)
                print(f"PROVA {codice}: contenitore pronto, NON pubblicato.")
            except Exception as e:
                print(f"PROVA {codice}: ERRORE {e}")
            continue
        if dt.datetime.now(dt.timezone.utc) - quando > RITARDO_MASSIMO:
            salva("errori", codice, {"errore": "ora passata da più di 6 ore: non pubblicato", "quando": post["quando"]})
            shutil.rmtree(cartella)
            continue
        try:
            contenitori = contenitore(uid, post)
        except Exception as e:
            print(f"{codice}: errore nella preparazione, riprovo al prossimo giro: {e}")
            continue
        attesa = (quando - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if attesa > 0:
            time.sleep(attesa)
        # teaser e storia racconto escono solo se il loro post è uscito davvero
        if post.get("dopo") and not os.path.exists(f"fatti/{post['dopo']}.json"):
            salva("errori", codice, {"errore": f"il post {post['dopo']} non è uscito: storie non pubblicate",
                                     "quando": post["quando"]})
            shutil.rmtree(cartella)
            continue
        try:
            pubblicati = []
            for c in contenitori:
                pubblicati.append(ig(f"{uid}/media_publish", {"creation_id": c}, metodo="POST")["id"])
                time.sleep(5)                 # le schermate di una storia escono in ordine
            try:
                link = ig(pubblicati[0], {"fields": "permalink"}).get("permalink")
            except RuntimeError:
                link = None
            salva("fatti", codice, {"id": pubblicati[0], "id_tutti": pubblicati, "link": link, "quando": post["quando"],
                                    "pubblicato": dt.datetime.now(dt.timezone.utc).isoformat()})
            print(f"PUBBLICATO {codice}: {link}")
        except Exception as e:
            # niente nuovo tentativo: se Instagram l'avesse pubblicato lo stesso, uscirebbe due volte
            salva("errori", codice, {"errore": str(e), "quando": post["quando"]})
            print(f"{codice}: ERRORE nella pubblicazione: {e}")
        shutil.rmtree(cartella)
    try:
        rinnova_token()
    except Exception as e:
        print(f"Rinnovo del token non riuscito: {e}")


if __name__ == "__main__":
    main()
