#!/usr/bin/env python3
"""
Resultados de Presidente e Governador do DF por zona eleitoral, bairro e seção.

Usa APENAS arquivos oficiais do Portal de Dados Abertos do TSE (nada de raspagem):

  1) Votação por seção eleitoral (um zip por UF), ex.:
       votacao_secao_2026_DF.zip
     Baixe em https://dadosabertos.tse.jus.br  (conjunto "Resultados - 2026").
     Atenção: em 05/10/2026 esse arquivo AINDA NÃO estava publicado para 2026.

  2) Eleitorado por local de votação (traz o BAIRRO de cada seção), ex.:
       eleitorado_local_votacao_2026.zip
     https://cdn.tse.jus.br/estatistica/sead/odsele/eleitorado_locais_votacao/eleitorado_local_votacao_2026.zip

Uso:
  python3 resultados_df.py --votacao votacao_secao_2026_DF.zip \
      --locais eleitorado_local_votacao_2026.zip --turno 1 --saida saida/

Requer: Python 3.9+ e pandas (pip install pandas).

Saídas (uma família de arquivos por cargo):
  <cargo>_por_secao.csv   zona, bairro, local, seção, votos por candidato, brancos, nulos, %
  <cargo>_por_bairro.csv  somado por bairro
  <cargo>_por_zona.csv    somado por zona
  <cargo>_por_zona_bairro.csv  somado por zona + bairro
  alertas.txt             seções sem bairro, colunas não encontradas etc.
"""
import argparse
import io
import os
import sys
import zipfile

import pandas as pd

CARGOS = {1: "presidente", 3: "governador"}
BRANCO, NULO = 95, 96  # códigos de voto branco e nulo nos arquivos do TSE

# Nomes das zonas do DF, conforme informado pelo usuário (não vêm do TSE).
ZONAS = {
    1: "Asa Sul",
    2: "Paranoá (inclui Itapoã, Lago Norte e Varjão)",
    3: "Taguatinga Norte",
    4: "Santa Maria",
    5: "Sobradinho (inclui Sobradinho II e Fercal)",
    6: "Planaltina",
    7: "Extinta (remanejada)",
    8: "Ceilândia Centro / Norte",
    9: "Guará",
    10: "Núcleo Bandeirante (inclui Candangolândia e Riacho Fundo)",
    11: "Cruzeiro (inclui Sudoeste e Octogonal)",
    12: "Extinta (remanejada)",
    13: "Samambaia",
    14: "Asa Norte",
    15: "Águas Claras",
    16: "Ceilândia Norte e Brazlândia",
    17: "Gama",
    18: "Lago Sul (inclui São Sebastião e Jardim Botânico)",
    19: "Taguatinga Norte / Vicente Pires",
    20: "Ceilândia Sul",
    21: "Recanto das Emas",
}


def nome_zona(z):
    return ZONAS.get(int(z), "Zona não listada")


def abrir_csv(caminho, preferir="DF"):
    """Devolve um objeto de texto aberto para o CSV (direto ou dentro de um zip)."""
    if caminho.lower().endswith(".zip"):
        z = zipfile.ZipFile(caminho)
        csvs = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not csvs:
            sys.exit(f"Nenhum CSV dentro de {caminho}")
        # prefere o CSV do DF; se só houver um, usa ele
        alvo = next((n for n in csvs if f"_{preferir}." in n.upper()), csvs[0])
        return z.open(alvo), alvo
    return open(caminho, "rb"), os.path.basename(caminho)


def achar(colunas, *candidatos):
    """Acha o nome real da coluna ignorando maiúsculas/minúsculas."""
    mapa = {c.upper(): c for c in colunas}
    for c in candidatos:
        if c.upper() in mapa:
            return mapa[c.upper()]
    return None


def ler(caminho, encoding="latin-1", **kw):
    f, nome = abrir_csv(caminho)
    return pd.read_csv(f, sep=";", encoding=encoding, dtype=str,
                       quotechar='"', **kw), nome


def carregar_locais(caminho, alertas):
    """Tabela (zona, seção) -> bairro, local, endereço. Só DF."""
    df, nome = ler(caminho)
    c_uf = achar(df.columns, "SG_UF")
    if c_uf:
        df = df[df[c_uf].str.upper() == "DF"]
    c_zona = achar(df.columns, "NR_ZONA")
    c_sec = achar(df.columns, "NR_SECAO")
    c_bairro = achar(df.columns, "NM_BAIRRO")
    c_local = achar(df.columns, "NM_LOCAL_VOTACAO")
    c_end = achar(df.columns, "DS_ENDERECO", "DS_LOCAL_VOTACAO_ENDERECO")
    faltam = [n for n, c in [("NR_ZONA", c_zona), ("NR_SECAO", c_sec),
                             ("NM_BAIRRO", c_bairro)] if c is None]
    if faltam:
        sys.exit(f"{nome}: colunas obrigatórias ausentes: {faltam}. "
                 f"Colunas encontradas: {list(df.columns)}")
    out = pd.DataFrame({
        "zona": df[c_zona].astype(int),
        "secao": df[c_sec].astype(int),
        "bairro": df[c_bairro].fillna("").str.strip().str.upper(),
        "local_votacao": df[c_local].fillna("").str.strip() if c_local else "",
        "endereco": df[c_end].fillna("").str.strip() if c_end else "",
    })
    antes = len(out)
    out = out.drop_duplicates(["zona", "secao"])
    if len(out) != antes:
        alertas.append(f"locais: {antes - len(out)} linhas duplicadas por (zona, seção) descartadas")
    return out


def carregar_votos(caminho, turno, alertas):
    f, nome = abrir_csv(caminho)
    cabecalho = pd.read_csv(f, sep=";", encoding="latin-1", nrows=0)
    cols = list(cabecalho.columns)
    f.close()

    def obrig(*n):
        c = achar(cols, *n)
        if c is None:
            sys.exit(f"{nome}: coluna obrigatória ausente {n}. Colunas: {cols}")
        return c

    c_uf = achar(cols, "SG_UF")
    c_turno = obrig("NR_TURNO")
    c_cargo = obrig("CD_CARGO")
    c_zona = obrig("NR_ZONA")
    c_sec = obrig("NR_SECAO")
    c_num = obrig("NR_VOTAVEL")
    c_nome = obrig("NM_VOTAVEL")
    c_qt = obrig("QT_VOTOS")
    c_local = achar(cols, "NM_LOCAL_VOTACAO")
    c_end = achar(cols, "DS_LOCAL_VOTACAO_ENDERECO")
    usar = [c for c in [c_uf, c_turno, c_cargo, c_zona, c_sec, c_num, c_nome,
                        c_qt, c_local, c_end] if c]

    partes = []
    f, _ = abrir_csv(caminho)
    for chunk in pd.read_csv(f, sep=";", encoding="latin-1", dtype=str,
                             usecols=usar, chunksize=500_000):
        m = chunk[c_cargo].astype(int).isin(CARGOS) & (chunk[c_turno].astype(int) == turno)
        if c_uf:
            m &= chunk[c_uf].str.upper() == "DF"
        chunk = chunk[m]
        if len(chunk):
            partes.append(pd.DataFrame({
                "cargo": chunk[c_cargo].astype(int).map(CARGOS),
                "zona": chunk[c_zona].astype(int),
                "secao": chunk[c_sec].astype(int),
                "nr": chunk[c_num].astype(int),
                "nome": chunk[c_nome].fillna("").str.strip(),
                "votos": chunk[c_qt].astype(int),
                "local_votacao_bu": chunk[c_local].fillna("") if c_local else "",
                "endereco_bu": chunk[c_end].fillna("") if c_end else "",
            }))
    if not partes:
        sys.exit("Nenhuma linha de Presidente/Governador do DF nesse turno. "
                 "Confira o arquivo e o --turno.")
    return pd.concat(partes, ignore_index=True)


def rotulo(nr, nome):
    if nr == BRANCO:
        return "BRANCOS"
    if nr == NULO:
        return "NULOS"
    return f"{nome.title()} ({nr})"


def montar(votos_cargo, locais, alertas, nome_cargo):
    v = votos_cargo.copy()
    v["rotulo"] = [rotulo(n, m) for n, m in zip(v["nr"], v["nome"])]
    chave = ["zona", "secao"]

    larga = v.pivot_table(index=chave, columns="rotulo", values="votos",
                          aggfunc="sum", fill_value=0)
    larga = larga.reset_index()

    # nomes de local/endereço presentes no próprio arquivo de votos
    aux = (v[chave + ["local_votacao_bu", "endereco_bu"]]
           .drop_duplicates(chave))
    larga = larga.merge(aux, on=chave, how="left")
    larga = larga.merge(locais, on=chave, how="left")

    sem_bairro = larga["bairro"].isna() | (larga["bairro"] == "")
    if sem_bairro.any():
        alertas.append(f"{nome_cargo}: {int(sem_bairro.sum())} seções sem bairro no arquivo de locais "
                       f"(ficam como 'SEM BAIRRO')")
    larga["bairro"] = larga["bairro"].fillna("").replace("", "SEM BAIRRO")
    larga["local_votacao"] = larga["local_votacao"].where(
        larga["local_votacao"].notna() & (larga["local_votacao"] != ""),
        larga["local_votacao_bu"])
    larga["endereco"] = larga["endereco"].where(
        larga["endereco"].notna() & (larga["endereco"] != ""),
        larga["endereco_bu"])
    larga = larga.drop(columns=["local_votacao_bu", "endereco_bu"])
    return larga


def somar(larga, por, cols_votos):
    g = larga.groupby(por, as_index=False)[cols_votos].sum()
    g.insert(len(por), "secoes", larga.groupby(por).size().values)
    return finalizar(g, cols_votos)


def finalizar(df, cols_votos):
    if "zona" in df.columns:
        df.insert(df.columns.get_loc("zona") + 1, "zona_nome", df["zona"].map(nome_zona))
    cand = [c for c in cols_votos if c not in ("BRANCOS", "NULOS")]
    df["VOTOS_VALIDOS"] = df[cand].sum(axis=1)
    df["BRANCOS"] = df.get("BRANCOS", 0)
    df["NULOS"] = df.get("NULOS", 0)
    df["TOTAL_COMPARECIMENTO"] = df["VOTOS_VALIDOS"] + df["BRANCOS"] + df["NULOS"]
    for c in cand:
        df[f"%validos {c}"] = (100 * df[c] / df["VOTOS_VALIDOS"].where(df["VOTOS_VALIDOS"] > 0)).round(2)
    return df


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--votacao", required=True, help="votacao_secao_2026_DF (.zip ou .csv)")
    ap.add_argument("--locais", required=True, help="eleitorado_local_votacao_2026 (.zip ou .csv)")
    ap.add_argument("--turno", type=int, default=1, choices=[1, 2])
    ap.add_argument("--saida", default="saida")
    a = ap.parse_args()

    os.makedirs(a.saida, exist_ok=True)
    alertas = []
    locais = carregar_locais(a.locais, alertas)
    votos = carregar_votos(a.votacao, a.turno, alertas)

    for nome in CARGOS.values():
        vc = votos[votos["cargo"] == nome]
        if vc.empty:
            alertas.append(f"{nome}: sem linhas no arquivo para o turno {a.turno}")
            continue
        larga = montar(vc, locais, alertas, nome)
        cols_votos = [c for c in larga.columns if c not in
                      ("zona", "secao", "bairro", "local_votacao", "endereco")]
        secao = finalizar(larga.copy(), cols_votos)
        extras = sorted(set(secao["zona"]) - set(ZONAS))
        if extras:
            alertas.append(f"{nome}: zonas nos dados que não estão na lista de nomes: {extras}")
        ordem = ["zona", "zona_nome", "bairro", "local_votacao", "endereco", "secao"]
        resto = [c for c in secao.columns if c not in ordem]
        secao[ordem + resto].sort_values(["zona", "bairro", "secao"]).to_csv(
            f"{a.saida}/{nome}_t{a.turno}_por_secao.csv", index=False, sep=";", encoding="utf-8-sig")
        somar(larga, ["bairro"], cols_votos).sort_values("bairro").to_csv(
            f"{a.saida}/{nome}_t{a.turno}_por_bairro.csv", index=False, sep=";", encoding="utf-8-sig")
        somar(larga, ["zona"], cols_votos).sort_values("zona").to_csv(
            f"{a.saida}/{nome}_t{a.turno}_por_zona.csv", index=False, sep=";", encoding="utf-8-sig")
        somar(larga, ["zona", "bairro"], cols_votos).sort_values(["zona", "bairro"]).to_csv(
            f"{a.saida}/{nome}_t{a.turno}_por_zona_bairro.csv", index=False, sep=";", encoding="utf-8-sig")
        tot = secao["TOTAL_COMPARECIMENTO"].sum()
        print(f"{nome}: {len(secao)} seções, {int(tot):,} votos (válidos+brancos+nulos)".replace(",", "."))

    with open(f"{a.saida}/alertas.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(alertas) or "sem alertas")
    if alertas:
        print("ALERTAS:\n  " + "\n  ".join(alertas))
    print(f"Arquivos gravados em {a.saida}/")


if __name__ == "__main__":
    main()
