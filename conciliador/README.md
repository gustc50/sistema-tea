# Conciliador Bancário → Domínio Contábil

Importa extratos bancários em **OFX** e **PDF**, classifica cada transação com
base no plano de contas da empresa e em regras de situações especiais, e gera o
arquivo **TXT de importação de lançamentos do Domínio Contábil**.

Bancos suportados: **Sicoob, Sicredi, Itaú, Inter, Caixa Econômica Federal,
Banco do Brasil e Santander** (o banco é detectado automaticamente pelo
arquivo; outros bancos costumam funcionar via OFX, que é um formato padrão).

## Como iniciar

Dê dois cliques em **`Iniciar_Conciliador.bat`** (na raiz do projeto), ou:

```
pip install flask pdfplumber
cd conciliador
python app.py
```

Acesse **http://127.0.0.1:5001** (o Sistema TEA continua na porta 5000).
Os dados ficam em `conciliador/conciliador.db` (SQLite, criado automaticamente).

## Multiempresa (clientes do escritório)

O conciliador é **multiempresa**: cada empresa cadastrada tem seu próprio
plano de contas, contas bancárias, regras, memória de classificação,
histórico de importações e configurações. A empresa ativa é escolhida no
seletor do menu lateral, e novas empresas são cadastradas na aba
**Configurações**. Bancos de dados criados na versão anterior (empresa
única) são migrados automaticamente: os dados existentes viram a
"Empresa 1", sem perda de nada.

## Passo a passo de uso

1. **Configurações** — dê nome à empresa e informe o CNPJ (obrigatório para
   gerar o arquivo), o código da filial no Domínio (em branco = matriz), os
   códigos de histórico padrão para entradas e saídas e, se quiser, uma
   **conta padrão** para transações não identificadas (ex.: conta
   transitória). Cadastre as demais empresas (clientes) na mesma aba.
2. **Plano de Contas** — cole o plano da empresa (uma conta por linha, no
   formato `código;descrição` — aceita também TAB ou vírgula, direto do Excel).
   O código é o **código da conta usado nos lançamentos do Domínio**.
3. **Contas Bancárias** — cadastre cada conta bancária da empresa e a conta
   contábil correspondente no plano. Informar agência/nº da conta permite ao
   sistema detectar automaticamente a conta ao processar um extrato.
4. **Regras Especiais** — as "situações especiais": quando a descrição da
   transação contiver certos termos (ex.: `TARIFA;TAR PACOTE`), for entrada ou
   saída, estiver numa faixa de valor ou numa conta bancária específica, o
   lançamento vai para a conta indicada, com o histórico e o complemento
   definidos na regra. A regra de menor prioridade numérica é avaliada antes;
   a primeira que casar vence.
5. **Conciliar Extrato** — envie o `.ofx` ou `.pdf`, confira a classificação
   sugerida (tudo é editável linha a linha), desmarque o que não deve ser
   lançado e clique em **Gerar arquivo Domínio (.txt)**. Transações já
   exportadas antes aparecem em amarelo ("já importada") e vêm desmarcadas.
   Ao preencher a conta contábil de uma linha, o sistema pergunta se aplica
   a mesma conta às demais transações com **descrição semelhante** (a
   comparação ignora números e datas — útil para retiradas/aportes de
   sócios que se repetem no mês). Para tornar a classificação permanente
   nos próximos extratos, use o botão **＋regra** da linha.

   A classificação segue esta ordem de prioridade em cada transação:
   **1) regras especiais → 2) memória → 3) conta padrão**. A *memória*
   sugere a conta e o histórico usados para a mesma descrição em
   exportações anteriores (etiqueta "🧠 memória") — ou seja, o que você
   classificar manualmente uma vez volta preenchido nos meses seguintes,
   mesmo sem regra. Os filtros acima da tabela (Todas / Sem conta /
   Entradas / Saídas / Já importadas), o marcar/desmarcar todas no
   cabeçalho e o contador "Sem conta" no rodapé ajudam a conferir
   extratos grandes rapidamente.

No Domínio, importe o TXT em **Utilitários > Importação > Importação Padrão
de Lançamentos**.

## Formato do arquivo gerado

Layout de lançamentos contábeis do Domínio, codificação Windows-1252:

```
|0000|CNPJ (só dígitos)|
|6000|X|||
|6100|DD/MM/AAAA|conta débito|conta crédito|valor|cód. histórico|complemento||filial|
```

Cada transação vira um lançamento de partida dupla:

| Movimento no extrato | Débito | Crédito |
|---|---|---|
| Entrada (crédito) | conta contábil do banco | conta de contrapartida |
| Saída (débito) | conta de contrapartida | conta contábil do banco |

O complemento aceita os modelos `{descricao}`, `{documento}`, `{data}` e
`{valor}`, configuráveis por regra e nas Configurações.

## Observações sobre PDF

- O PDF precisa conter **texto** (extrato baixado do internet banking/app).
  Extratos **escaneados (imagem)** não são lidos — nesse caso use o OFX.
- Layouts de extrato variam entre versões dos aplicativos dos bancos. O
  parser trata os padrões usuais: valor com sufixo D/C (colado ou separado),
  valor com sinal antes ou depois, com e sem separador de milhar, data
  completa ou curta (dd/mm, com ano deduzido do período), datas agrupadas
  por dia (Inter e extratos de app), histórico em várias linhas, texto com
  letras espaçadas e linhas de saldo ignoradas. A tela de conferência
  permite corrigir qualquer linha antes de exportar.
- **Conferência de saldo**: quando o extrato traz saldo inicial e final, o
  sistema confere se `saldo inicial + movimento lido = saldo final`. Se não
  bater, um alerta mostra a diferença exata — sinal de que alguma linha do
  PDF não foi reconhecida — e o botão "ver o texto lido" ajuda a localizar
  o lançamento que falta comparando com o extrato original. Extrato íntegro
  ganha o selo "✓ confere". Confira o selo antes de exportar.
- **Diagnóstico**: se nenhuma transação for reconhecida, a tela mostra o
  texto que foi extraído do PDF. Se as transações aparecem nesse texto,
  copie-o e envie para o suporte ajustar o leitor àquele layout; se o texto
  estiver vazio, o PDF é imagem escaneada (use o OFX). PDFs protegidos por
  senha também não são lidos — salve uma cópia sem senha.
- Transações **idênticas no mesmo dia** (mesmo valor e descrição, comum em
  PIX de mesmo valor) são numeradas por ocorrência no controle de
  duplicidade — a segunda não é confundida com reimportação da primeira.
- Sempre que possível, **prefira o OFX**: é um formato estruturado, com
  identificador único por transação (controle de duplicidade mais preciso).
