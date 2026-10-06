#!/usr/bin/env python3
"""
Coleta os boletins de urna (BU) do DF no site de resultados do TSE e gera um CSV
de votos por seção para PRESIDENTE e GOVERNADOR, no mesmo formato do arquivo
"votacao_secao" dos dados abertos. Esse CSV alimenta o resultados_df.py, que
junta zona, bairro e seção.

Como funciona (verificado em 05/10/2026 com a seção 0001 da zona 0001):
  1. config/df/df-pPPPPPP-cs.jws        -> lista de zonas e seções do DF
  2. dados/df/<mun>/<zona>/<seção>/...-aux.jws -> hash e nome do arquivo do BU
  3. dados/df/<mun>/<zona>/<seção>/<hash>/...-bu.dat -> boletim (ASN.1/BER)
  4. dados/df/df97012-c0001-e006257-u.jws e ...-c0003-e006259-u.jws
        -> nomes dos candidatos e totais do município (usados para conferir)

Educação com o servidor (não retire):
  * poucas requisições em paralelo (máx. 4) e pausa entre elas;
  * tudo que é baixado fica em cache/ (rodar de novo não baixa de novo);
  * obedece o robots.txt do TSE, se ele existir;
  * para na hora se o servidor recusar (HTTP 403/429) ou falhar muitas vezes.
  São cerca de 7 mil seções (14 mil requisições). Com os padrões, leva ~1 a 2 h.

Uso:
  python3 coletar_bu_df.py --limite 5                # teste com 5 seções
  python3 coletar_bu_df.py                           # coleta tudo
  python3 coletar_bu_df.py --decodificar arq-bu.dat  # decodifica um arquivo só

Depois:
  python3 resultados_df.py --votacao saida_bu/votacao_secao_coletada_2026_DF.csv \
      --locais eleitorado_local_votacao_2026.zip --turno 1

Só biblioteca padrão do Python (3.9+).
"""
import argparse
import base64
import csv
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor

HOST = "https://resultados.tse.jus.br"
UF, MUN = "df", "97012"
CARGOS = {1: "Presidente", 3: "Governador"}
TIPO_NOMINAL, TIPO_BRANCO, TIPO_NULO = 1, 2, 3
UA = "coletor-bu-df/1.0 (analise pessoal de resultados publicos)"


# ----------------------------------------------------------------- BER / ASN.1
class No:
    __slots__ = ("cls", "tag", "filhos", "bruto")

    def __init__(self, cls, tag, filhos, bruto):
        self.cls, self.tag, self.filhos, self.bruto = cls, tag, filhos, bruto

    def inteiro(self):
        return int.from_bytes(self.bruto, "big", signed=True)

    def texto(self):
        return self.bruto.decode("utf-8", "replace")


def ber(buf, p=0, fim=None):
    """Decodifica BER genérico (tag-tamanho-valor) em uma lista de No."""
    fim = len(buf) if fim is None else fim
    out = []
    while p < fim:
        t = buf[p]
        p += 1
        cls, cons, tag = t >> 6, (t >> 5) & 1, t & 31
        if tag == 31:
            tag = 0
            while True:
                x = buf[p]
                p += 1
                tag = (tag << 7) | (x & 127)
                if not x & 128:
                    break
        n = buf[p]
        p += 1
        if n & 128:
            k = n & 127
            if k == 0:
                raise ValueError("tamanho indefinido não suportado")
            n = int.from_bytes(buf[p:p + k], "big")
            p += k
        if p + n > fim:
            raise ValueError("arquivo truncado ou corrompido")
        if cons:
            out.append(No(cls, tag, ber(buf, p, p + n), None))
        else:
            out.append(No(cls, tag, None, bytes(buf[p:p + n])))
        p += n
    return out


def _ctx(n, tag):  # nó primitivo de classe "contexto" com a tag dada
    return n.cls == 2 and n.tag == tag and n.filhos is None


def _eh_item(n):
    return (n.filhos is not None and any(_ctx(k, 1) for k in n.filhos)
            and any(_ctx(k, 2) for k in n.filhos))


def _achar_cargos(n, achados):
    if n.filhos is None:
        return
    f = n.filhos
    if (len(f) >= 3 and _ctx(f[0], 1) and f[1].cls == 0 and f[1].tag == 2
            and f[2].filhos and all(_eh_item(x) for x in f[2].filhos)):
        achados[f[0].inteiro()] = f[2].filhos
        return
    for x in f:
        _achar_cargos(x, achados)


def decodificar_bu(dados):
    """Devolve {'zona','secao','local','votos':{cargo:[(tipo, numero, votos)]}}."""
    envelope = ber(dados)[0]
    ultimo = envelope.filhos[-1]
    if not (ultimo.cls == 0 and ultimo.tag == 4):
        raise ValueError("envelope do BU em formato inesperado")
    interno = ber(ultimo.bruto)[0]

    ident = next((k for k in interno.filhos
                  if k.filhos and len(k.filhos) == 3 and k.filhos[0].filhos
                  and len(k.filhos[0].filhos) == 2), None)
    zona = secao = local = None
    if ident:
        zona = ident.filhos[0].filhos[1].inteiro()
        local = ident.filhos[1].inteiro()
        secao = ident.filhos[2].inteiro()

    cargos = {}
    _achar_cargos(interno, cargos)
    votos = {}
    for cod in CARGOS:
        itens = cargos.get(cod)
        if itens is None:
            continue
        lista = []
        for it in itens:
            tipo = next(k for k in it.filhos if _ctx(k, 1)).inteiro()
            qt = next(k for k in it.filhos if _ctx(k, 2)).inteiro()
            cand = next((k for k in it.filhos
                         if k.cls == 2 and k.tag == 3 and k.filhos), None)
            numero = cand.filhos[-1].inteiro() if cand else None
            lista.append((tipo, numero, qt))
        votos[cod] = lista
    return {"zona": zona, "secao": secao, "local": local, "votos": votos}


# ----------------------------------------------------------------------- rede
class Recusado(Exception):
    pass


def baixar(url, tentativas=4):
    espera = 2.0
    for i in range(tentativas):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (403, 429):
                if i == tentativas - 1:
                    raise Recusado(f"HTTP {e.code} em {url}")
            elif e.code < 500 and e.code != 408:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(espera)
        espera *= 2
    raise RuntimeError(f"falhou após {tentativas} tentativas: {url}")


def jws(dados):
    """Payload (JSON) de um arquivo .jws; a assinatura NÃO é verificada aqui."""
    p = dados.decode("ascii").strip().split(".")[1]
    p += "=" * (-len(p) % 4)
    return json.loads(base64.urlsafe_b64decode(p).decode("utf-8"))


def checar_robots(url_base):
    rp = urllib.robotparser.RobotFileParser()
    try:
        r = baixar(HOST + "/robots.txt", tentativas=2)
    except Exception as e:  # noqa: BLE001
        print(f"aviso: robots.txt inacessível ({e}); seguindo com cautela.")
        return
    if not r or r.lstrip().lower().startswith(b"<html"):
        print("aviso: o TSE não publica robots.txt legível (veio vazio/erro); seguindo com cautela.")
        return
    rp.parse(r.decode("utf-8", "replace").splitlines())
    if not rp.can_fetch(UA, url_base) or not rp.can_fetch("*", url_base):
        sys.exit("O robots.txt do TSE proíbe acesso automatizado a esse caminho. Parando.")


# ------------------------------------------------------------------- coleta
def coletar(args):
    pl = f"{args.pleito:06d}"
    base = f"{HOST}/oficial/ele2026/arquivo-urna/{args.pleito}"
    checar_robots(base)

    cfg = baixar(f"{base}/config/{UF}/{UF}-p{pl}-cs.jws")
    if cfg is None:
        sys.exit("lista de zonas/seções não encontrada (confira --pleito)")
    cfg = jws(cfg)
    zonas = cfg["abr"][0]["mu"][0]["zon"]
    secoes = [(z["cd"], s["ns"]) for z in zonas for s in z["sec"]]
    if args.zonas:
        pedidas = {f"{int(z):04d}" for z in args.zonas.split(",")}
        secoes = [x for x in secoes if x[0] in pedidas]
    if args.limite:
        secoes = secoes[:args.limite]
    print(f"{len(secoes)} seções na lista do TSE; cache em {args.cache}/")
    os.makedirs(args.cache, exist_ok=True)

    falhas = {"consecutivas": 0, "sem_boletim": [], "erro": None}
    trava = threading.Lock()
    feitos = [0]

    def uma(par):
        zona, sec = par
        arq = os.path.join(args.cache, f"{zona}-{sec}.bu")
        if os.path.exists(arq) or falhas["erro"]:
            return
        pref = f"{base}/dados/{UF}/{MUN}/{zona}/{sec}"
        try:
            aux = baixar(f"{pref}/p{pl}-{UF}-m{MUN}-z{zona}-s{sec}-aux.jws")
            if aux is None:
                raise FileNotFoundError("sem aux")
            info = jws(aux)
            hs = info.get("hashes") or []
            hs = sorted(hs, key=lambda h: h.get("st") != "Totalizado")
            alvo = None
            for h in hs:
                for a in h.get("arq", []):
                    if a.get("tp") == "bu":
                        alvo = (h["hash"], a["nm"])
                        break
                if alvo:
                    break
            if not alvo:
                raise FileNotFoundError("aux sem BU")
            time.sleep(args.pausa)
            bu = baixar(f"{pref}/{alvo[0]}/{alvo[1]}")
            if bu is None:
                raise FileNotFoundError("BU 404")
            tmp = arq + ".tmp"
            with open(tmp, "wb") as f:
                f.write(bu)
            os.replace(tmp, arq)
            with trava:
                falhas["consecutivas"] = 0
        except FileNotFoundError:
            with trava:
                falhas["sem_boletim"].append(f"{zona}-{sec}")
        except Recusado as e:
            falhas["erro"] = f"O servidor recusou o acesso ({e}). Parei para não insistir."
        except Exception as e:  # noqa: BLE001
            with trava:
                falhas["consecutivas"] += 1
                if falhas["consecutivas"] >= 20:
                    falhas["erro"] = f"20 falhas seguidas (última: {e}). Parei."
        finally:
            with trava:
                feitos[0] += 1
                if feitos[0] % 100 == 0:
                    print(f"  {feitos[0]}/{len(secoes)}")
            time.sleep(args.pausa)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(uma, secoes))

    if falhas["sem_boletim"]:
        with open(os.path.join(args.saida, "sem_boletim.txt"), "w") as f:
            f.write("\n".join(falhas["sem_boletim"]))
        print(f"aviso: {len(falhas['sem_boletim'])} seções sem boletim publicado "
              f"(lista em sem_boletim.txt)")
    if falhas["erro"]:
        print("ATENÇÃO:", falhas["erro"], "\nO que já foi baixado está no cache; "
              "rode de novo mais tarde para continuar.")
    return secoes


def nomes_e_totais(args):
    """{cargo: ({numero: nome}, {numero: votos_município})} a partir dos agregados."""
    fontes = {1: f"{HOST}/oficial/ele2026/{args.eleicao_federal}/dados/{UF}/"
                 f"{UF}{MUN}-c0001-e{args.eleicao_federal:06d}-u.jws",
              3: f"{HOST}/oficial/ele2026/{args.eleicao_estadual}/dados/{UF}/"
                 f"{UF}{MUN}-c0003-e{args.eleicao_estadual:06d}-u.jws"}
    res = {}
    for cargo, url in fontes.items():
        nomes, totais = {}, {}
        try:
            d = baixar(url)
            dados = jws(d) if d else {}
        except Exception as e:  # noqa: BLE001
            print(f"aviso: não consegui os nomes de candidatos de {CARGOS[cargo]}: {e}")
            dados = {}

        def anda(o):
            if isinstance(o, dict):
                if "nmu" in o and "n" in o:
                    nomes[int(o["n"])] = o["nmu"]
                    if "vap" in o:
                        totais[int(o["n"])] = int(o["vap"])
                for v in o.values():
                    anda(v)
            elif isinstance(o, list):
                for v in o:
                    anda(v)
        anda(dados)
        res[cargo] = (nomes, totais)
    return res


def gerar_csv(args, secoes):
    nt = nomes_e_totais(args)
    somas = {c: {} for c in CARGOS}
    os.makedirs(args.saida, exist_ok=True)
    destino = os.path.join(args.saida, "votacao_secao_coletada_2026_DF.csv")
    cab = ["ANO_ELEICAO", "NR_TURNO", "SG_UF", "CD_MUNICIPIO", "NR_ZONA", "NR_SECAO",
           "NR_LOCAL_VOTACAO", "CD_CARGO", "DS_CARGO", "NR_VOTAVEL", "NM_VOTAVEL", "QT_VOTOS"]
    n_ok = n_diverge = 0
    with open(destino, "w", newline="", encoding="latin-1", errors="replace") as f:
        w = csv.writer(f, delimiter=";", quotechar='"', quoting=csv.QUOTE_ALL)
        w.writerow(cab)
        for zona, sec in secoes:
            arq = os.path.join(args.cache, f"{zona}-{sec}.bu")
            if not os.path.exists(arq):
                continue
            try:
                bu = decodificar_bu(open(arq, "rb").read())
            except Exception as e:  # noqa: BLE001
                print(f"aviso: BU {zona}-{sec} ilegível ({e})")
                continue
            if bu["zona"] is not None and (bu["zona"], bu["secao"]) != (int(zona), int(sec)):
                n_diverge += 1
            n_ok += 1
            for cargo, itens in bu["votos"].items():
                nomes = nt[cargo][0]
                for tipo, numero, qt in itens:
                    if tipo == TIPO_NOMINAL:
                        nr, nm = numero, nomes.get(numero, f"Candidato {numero}")
                        somas[cargo][nr] = somas[cargo].get(nr, 0) + qt
                    elif tipo == TIPO_BRANCO:
                        nr, nm = 95, "Branco"
                    elif tipo == TIPO_NULO:
                        nr, nm = 96, "Nulo"
                    else:
                        continue
                    w.writerow([2026, args.turno, "DF", MUN, int(zona), int(sec),
                                bu["local"] or "", cargo, CARGOS[cargo], nr, nm, qt])
    print(f"\n{n_ok} boletins decodificados -> {destino}")
    if n_diverge:
        print(f"aviso: {n_diverge} boletins com zona/seção diferente da lista (conferir)")

    print("\nConferência com o total oficial do município (candidatos):")
    for cargo in CARGOS:
        oficial = nt[cargo][1]
        soma = somas[cargo]
        if not oficial:
            print(f"  {CARGOS[cargo]}: sem total oficial para comparar")
            continue
        dif = {n: soma.get(n, 0) - oficial.get(n, 0)
               for n in set(soma) | set(oficial)
               if soma.get(n, 0) != oficial.get(n, 0)}
        tot_of, tot_so = sum(oficial.values()), sum(soma.values())
        estado = "BATE" if not dif else "DIFERE (esperado se a coleta foi parcial)"
        print(f"  {CARGOS[cargo]}: coletado {tot_so:,} x oficial {tot_of:,} -> {estado}"
              .replace(",", "."))
        for n, d in sorted(dif.items(), key=lambda x: -abs(x[1]))[:5]:
            nome = nt[cargo][0].get(n, f"candidato {n}")
            print(f"      {nome}: diferença de {d:+d}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--decodificar", metavar="ARQ-bu.dat",
                    help="só decodifica um BU local e mostra os votos")
    ap.add_argument("--pleito", type=int, default=3220, help="código do pleito (1º turno 2026 = 3220)")
    ap.add_argument("--eleicao-federal", type=int, default=6257)
    ap.add_argument("--eleicao-estadual", type=int, default=6259)
    ap.add_argument("--turno", type=int, default=1)
    ap.add_argument("--zonas", help="ex.: 1,2,15 (padrão: todas)")
    ap.add_argument("--limite", type=int, help="processa só as N primeiras seções (teste)")
    ap.add_argument("--workers", type=int, default=2, help="requisições paralelas (máx. 4)")
    ap.add_argument("--pausa", type=float, default=0.3, help="segundos entre requisições por worker")
    ap.add_argument("--cache", default="cache_bu")
    ap.add_argument("--saida", default="saida_bu")
    a = ap.parse_args()
    a.workers = max(1, min(a.workers, 4))
    a.pausa = max(a.pausa, 0.1)

    if a.decodificar:
        r = decodificar_bu(open(a.decodificar, "rb").read())
        print(f"zona {r['zona']} seção {r['secao']} local {r['local']}")
        for cargo, itens in r["votos"].items():
            print(CARGOS[cargo])
            for tipo, numero, qt in itens:
                rot = {1: f"candidato {numero}", 2: "branco", 3: "nulo"}.get(tipo, f"tipo {tipo}")
                print(f"  {rot}: {qt}")
        return

    os.makedirs(a.saida, exist_ok=True)
    secoes = coletar(a)
    gerar_csv(a, secoes)


if __name__ == "__main__":
    main()
