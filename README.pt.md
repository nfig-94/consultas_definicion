# Definition Queries

[English](README.md) · [Español](README.es.md) · **Português**

**Filtre suas camadas no QGIS sem escrever SQL.** Escolha campo, operador e valores em listas suspensas (com os valores reais da camada) e o complemento monta a consulta para você.

O QGIS já permite filtrar camadas com *Filtrar…*, mas é preciso escrever a expressão SQL à mão e cada camada guarda um só filtro. Com o Definition Queries:

- **Sem SQL:** construtor visual com listas de caixas de seleção, E / OU, grupos e subgrupos.
- **Várias consultas com nome por camada**, salvas no projeto, trocadas com um clique direito.
- **Avisos antes de filtrar errado** (valor mal escrito, número ambíguo, filtro que não deixa nada, campo que mudou de nome).

QGIS 3.34 ou superior. Interface em inglês, espanhol e português (segue o idioma do QGIS).

![Construtor visual: filtrar sem escrever SQL](docs/images/02_no_sql.png)

*Imagens de exemplo: países do mundo do Natural Earth (domínio público) com CO₂ por pessoa do Our World in Data (CC BY 4.0).*

## O que faz

- **Várias consultas com nome por camada**, salvas dentro do projeto (.qgz).
- **Troque de consulta com clique direito na camada.** A ativa fica marcada com ✓.
- **Construtor visual**: `Onde [campo ▾] [operador ▾] [valor ▾]`, com os valores reais da camada, listas com busca, e grupos e subgrupos (parênteses).
- **Os operadores do ArcGIS**: é (não é) igual a, é um / nenhum de, contém / não contém, começa / não começa com, termina / não termina com, maior / menor que, está (não está) entre, está em branco, é nulo. Para datas: é em, não é em, é antes de, é depois de, é em ou antes de, é em ou depois de.
- **Modo SQL** quando precisar; ao voltar ao construtor, o SQL vira cláusulas quando possível.
- **Verificar** quantas feições cumprem a consulta antes de aplicá-la.
- **Filtrar ou selecionar** com a mesma consulta.
- **Selecionar feições visíveis**: seleciona o que você vê no mapa (com o filtro da camada, sem as categorias desligadas na legenda e seguindo o Controlador temporal), como no ArcGIS.
- **Filtrar pela seleção**, com um nome sugerido.
- **Avisos**: valor mal escrito, espaços a mais, um «1.000» ou «03/04/2025» ambíguo, «E» e «OU» misturados sem grupo, filtro que não deixa feições, e campos usados pelas consultas que não existem mais na camada (com uma ferramenta para substituí-los em todas as consultas).
- **Sincronizado com «Filtrar…» do QGIS**, **ferramentas de Processing** e **exportar / importar** consultas.

| | |
|---|---|
| ![Menu de clique direito](docs/images/03_one_click.png) | ![Antes e depois](docs/images/05_before_after.png) |
| ![E / OU e grupos](docs/images/04_groups.png) | ![Avisos antes de filtrar](docs/images/07_warnings.png) |

## O mesmo resultado em todos os formatos

O complemento escreve o SQL para cada motor (GeoPackage, Shapefile, GeoJSON, FlatGeobuf, FileGDB, Excel, SpatiaLite, CSV, camada temporária, camada virtual e PostGIS), para que as regras sejam as mesmas em todos:

- **«é igual a» / «é um de»**: correspondência exata (diferencia maiúsculas e acentos). Nas listas, espaços no início ou no fim aparecem como «␣», para distinguir «Vega» de «Vega ».
- **«contém» / «começa com» / «termina com»**: não diferencia maiúsculas, também em letras acentuadas. `_` e `%` não são curingas.
- **Valores vazios**: se o campo os tem, as listas de valores mostram «<Nulo>» (sem valor) e «<Vazio>» (um texto sem nada escrito), como no ArcGIS. As negações deixam os nulos de fora, como no SQL e no ArcGIS, e as contagens coincidem. Shapefile e CSV não guardam texto vazio: ali ele é lido como <Nulo>.
- **Datas**: num campo de data e hora, uma data sozinha é o dia inteiro: «é em 2024-03-03» encontra todas as horas desse dia e «é depois de 2024-03-03» começa no dia 4.
- **Um valor vazio não filtra**: a cláusula fica pendente até você escolher um valor.

Essas regras são verificadas pelos scripts em [`tests/`](tests/README.md), que qualquer pessoa pode executar. Detalhes na [versão em inglês](README.md#same-result-in-every-format).

## Instalação

QGIS → *Complementos → Gerenciar e instalar complementos* → procure «Definition Queries». Ou baixe `definition_queries.zip` em [Releases](https://github.com/nfig-94/definition_queries/releases) → *Instalar a partir do ZIP*.

## Uso rápido

1. Clique direito numa camada vetorial → **Definition Queries → Nova consulta…**
2. Escolha campo, operador e valor. Confira a pré-visualização e clique em **Verificar**.
3. **Aplicar filtro**. Para trocar de consulta, clique direito na camada e escolha outra.

## Bom saber

- As consultas ficam na camada dentro do projeto. Se você remover a camada e adicionar o arquivo de novo, elas não estarão lá (exporte-as antes, ou salve o estilo da camada: o .qml as guarda).
- Se o nome de um campo usado por uma consulta mudar, a camada fica sem feições e o complemento avisa. Corrija em *Gerenciar consultas… → ⋯ → Substituir um campo em todas as consultas*.
- Com milhares de feições selecionadas sem ordem, o filtro por seleção funciona, mas o mapa pode ficar lento; o complemento avisa.
- A nota nas camadas para quem não tem o complemento é opcional e vem desativada.
- Em Shapefile, GeoJSON e outros formatos do GDAL, uma lista de mais de 32 textos com letras não distingue maiúsculas de minúsculas (limitação do GDAL: a forma exata deixa o mapa muito lento). O complemento avisa quando isso muda o resultado.
- Outras fontes de dados (SQL Server, Oracle, WFS…) não foram testadas: o complemento avisa, e o banco de dados aplica as próprias regras. Confira o resultado com **Verificar**.

## Como foi feito

Desenvolvido por Nicolás Figueroa Arthur com a ajuda de um assistente de IA (Claude, da Anthropic). A lógica de filtragem é verificada com os testes em [`tests/`](tests/README.md). Testado no QGIS 3.34; a compatibilidade com o QGIS 4 foi verificada com o verificador Qt6 do repositório de complementos. Sugestões e problemas em [Issues](https://github.com/nfig-94/definition_queries/issues).

## Licença

GPL-2.0 ou posterior.
