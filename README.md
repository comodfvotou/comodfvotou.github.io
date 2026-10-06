# Como o DF votou

Resultado do 1º turno de 2026 no Distrito Federal (presidente e governador) por zona, bairro, local de votação e seção, com mapa.

Site: https://comodfvotou.github.io (arquivo `index.html`, mapa de ruas OpenStreetMap, precisa de internet).

## Conteúdo
- `coletar_bu_df.py`: baixa os boletins de urna (BU) do DF em resultados.tse.jus.br e gera `saida_bu/votacao_secao_coletada_2026_DF.csv`.
- `resultados_df.py`: junta os votos com zona, bairro e local (dados abertos do TSE) e gera as tabelas em `saida/`.

## Como refazer
```
python coletar_bu_df.py
python resultados_df.py --votacao saida_bu/votacao_secao_coletada_2026_DF.csv --locais eleitorado_local_votacao_2026.zip --turno 1
```
O zip dos locais de votação vem de https://cdn.tse.jus.br/estatistica/sead/odsele/eleitorado_locais_votacao/eleitorado_local_votacao_2026.zip. O resultado precisa de Python 3.9+ e pandas.

## Notas
- 6.969 seções com boletim. As outras 81 são seções agregadas, que votam junto com uma seção principal.
- Os totais coletados ficam acima do total oficial em 57 votos (presidente, candidato nº 28) e 8.456 votos (governador, candidato nº 55). O arquivo do TSE não traz o nome desses candidatos.
- Os nomes das zonas são referências geográficas aproximadas e não vêm do TSE.
- Fonte dos dados: TSE.
