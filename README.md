# F1 Telemetria

Recebe a telemetria UDP do **F1 24** (Xbox), grava cada volta e compara as voltas numa página local, com dicas por curva:
- quantos metros antes ou depois você freou;
- a velocidade no ápice;
- quando voltou a acelerar tudo;
- a linha (mais por dentro ou mais por fora), comparada com a volta de referência.

Só usa Python puro (stdlib), então roda no PC, num Raspberry ou no iPhone (a-Shell).

Testado em Interlagos, com voltas gravadas a 30 amostras por segundo, sem perda de pacotes.

## Rodar

```
py f1tel.py          # escuta a porta UDP 20777 e serve a página na porta 8000
py f1tel.py --teste  # confere a análise
```

Abra o endereço que ele imprime (`http://<ip>:8000`) no celular ou no PC. Na primeira vez, o Windows pergunta sobre o firewall: permita em **rede privada**.

## Xbox: F1 24 → Configurações → Telemetria

| Opção | Valor |
|---|---|
| Telemetria UDP | Ligada |
| Broadcast UDP | Desligado |
| Endereço IP | o IP que o `f1tel.py` imprime |
| Porta | 20777 |
| Frequência | 20 Hz |
| Formato UDP | 2024 |

O IP precisa ser fixo: reserve o IP do aparelho no roteador (reserva de DHCP).

**Não use o broadcast:** pelo Wi-Fi ele perdeu 98% dos pacotes. O IP direto chega completo.

## Rodar num Raspberry Pi (24/7)

Eu deixo o `f1tel.py` rodando num Raspberry Pi 4 ligado por cabo no roteador, para a página ficar sempre no ar.

- **Código:** `~/f1tel/`, com as voltas em `~/f1tel/voltas/`.
- **Serviço:** systemd de usuário com linger (`loginctl enable-linger`), então inicia no boot sem `sudo`. Para controlar: `systemctl --user restart|status f1tel`.
- **Atualizar:** `deploy.py` roda o pyflakes e o `--teste`, copia os arquivos por `scp` e reinicia o serviço. Ele lê o endereço do Pi da variável de ambiente `F1TEL_PI` (por exemplo `setx F1TEL_PI usuario@ip-do-pi` no Windows).

## Como lê as voltas

- Cada volta completa vira um JSON em `voltas/`. Voltas incompletas são descartadas (saída dos boxes, abandono).
- **Referência:** por padrão, a melhor volta válida da mesma pista.
- **Curva:** cada zona de frenagem da referência. Curvas feitas de pé no fundo não aparecem.
- **Linha:** é sempre relativa à volta de referência, porque o jogo não manda as bordas da pista.
- **Curvas:** pelo nome popular (S do Senna, Bico de Pato, Junção…), do `pistas.json`. Cada curva vai do começo da sua freada até o começo da freada da próxima, então a reta depois dela entra na conta dela. A reta dos boxes, que cruza a linha, fica com a última curva. Assim a soma de todas as curvas é exatamente a diferença da volta.
- **Fases:** a perda de cada curva é dividida em **freada** (até a referência soltar o freio), **até o ápice**, **saída** (até acelerar tudo) e **reta** (até a próxima freada). Cada dica fica na fase em que aconteceu; o que aconteceu numa fase que não perdeu tempo aparece apagado, porque não foi a causa. A dica falada e o engenheiro priorizam a fase que mais perdeu.
- **Rival:** no Contra-Relógio, a volta do fantasma do rival também é gravada (marcada como `rival`). O fantasma fica sempre com o mesmo número de volta, então a volta dele fecha quando a distância passa de perto do fim para perto de zero. Se o jogo não manda o tempo dele, o tempo de cada ponto é refeito pela velocidade.
- **Piloto:** cada volta grava o nome que o jogo manda no pacote Participants. Para vir a gamertag, ligue no jogo, em Configurações de telemetria, a opção de mostrar o nome (online ID / nomes dos jogadores). Na aba Voltas aparece um filtro por piloto. A referência, a melhor volta, os vícios e os botões usam sempre as voltas do mesmo piloto.

## Engenheiro e rádio

- **Análise da volta e vícios:** Claude Sonnet (`MODELO_IA`), via `claude -p` logado no Pi. O card **Vícios** (aba Resumo) junta as últimas 20 voltas válidas da pista contra a melhor e mostra o que se repete: % de voltas com cada erro, média e desvio por curva.
- **Rádio:** a dica curta é do Claude Haiku (`MODELO_RADIO`), porque precisa chegar rápido. A voz é do [Piper](https://github.com/rhasspy/piper) (voz neural pt-BR `faber`), rodando no Pi em `~/piper/`. A página aplica o efeito de rádio (`RADIO` no `index.html`). Sem Piper, usa a voz do aparelho.
- **Velocidade da fala:** `VOZ_RITMO` no `f1tel.py`.
- **Botões do controle** (no jogo: Configurações → Controles, mapeie "UDP Action 1" e "UDP Action 2" num botão livre). Precisa da página aberta com o rádio ligado.
  - **UDP Action 1:** marca o ponto onde você está ("Anotado."). No fim da volta, o engenheiro explica no rádio cada ponto marcado: quanto perdeu ou ganhou ali e por quê. A comparação é com o rival do Contra-Relógio, se houver; senão, com a sua melhor volta. Na página, a curva marcada aparece com 📍.
  - **UDP Action 2:** relatório curto da última volta: tempo, diferença para a melhor e a dica do Haiku.

## Bordas da pista

```
py bordas.py 16     # trackId do jogo; precisa de uma volta válida dessa pista em voltas/
```

O script baixa a pista real do [TUMFTM racetrack-database](https://github.com/TUMFTM/racetrack-database) (LGPL-3.0) e encaixa ela na sua melhor volta por rotação, deslocamento e escala, testando o espelho e o ponto de largada, com ajuste fino por ICP. As bordas ficam gravadas em `bordas/<trackId>.json`, e o mapa desenha a faixa de asfalto.

- **Pistas:** só as que existem no dataset (lista `TUMFTM` no script).
- **Conferência:** se a escala sair longe de 1, o traçado real mudou desde que o dataset foi feito.

## Ajustes

- **Limites das dicas** (`LIM_*`, no topo do `f1tel.py`): o mínimo de diferença para virar dica.
- **Posição das rodas** (`MEIA_BITOLA`, `EIXO`): se as zebras saírem deslocadas no mapa, ajuste aqui.
- **Zebras na borda** (`RAIO_ZEBRA`, padrão 2,5 m): quanto um contato de roda pode estar longe da borda para marcar aquele trecho como zebra. Se aparecer zebra onde não tem, diminua; se faltar, aumente.
- **Mapa espelhado:** troque `ESPELHAR` no `index.html` (já conferido em Interlagos: `true`).
- **Cor da linha no mapa:** escolha na lista acima do mapa. A página lembra a escolha.

## Como foi feito

Desenvolvido com o Claude Code: eu defino o que quero, testo no jogo e o Claude escreve e revisa o código. O `PLANO.md` registra o que já foi feito e o que falta.

## Licença

MIT © 2026 Arthur Schuck. As bordas das pistas (`bordas/`) são derivadas do [TUMFTM racetrack-database](https://github.com/TUMFTM/racetrack-database), sob a LGPL-3.0.

F1 é marca da Formula One Licensing B.V. Este é um projeto pessoal, sem ligação com a Formula 1, a EA ou a Codemasters.
