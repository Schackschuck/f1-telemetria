# Plano

1. [x] Script `f1tel.py`: receptor UDP, gravação, análise, servidor HTTP e `--teste`.
2. [x] Página `index.html`: dicas, mapa da pista e gráficos pela distância.
3. [x] Testar com o Xbox: as voltas são gravadas a 30 amostras/s, sem perda, em Interlagos (14/11/2026).
   - Broadcast pelo Wi-Fi perdeu 98% dos pacotes. Usar IP direto.
   - A busca da frenagem pegava a curva anterior ("freou 150 m antes"). Corrigido.
   - [ ] Frear muito cedo de propósito numa curva: a dica precisa apontar essa curva.
   - [ ] Fazer uma curva bem por dentro: a dica precisa dizer "por dentro". Conferir também se o mapa está espelhado.
   - [ ] Conferir se o fantasma do rival aparece nos pacotes, com voltas `[rival]`.
4. [x] Pi 4 no cabo do roteador, com serviço systemd de usuário.
5. [ ] Reservar o IP do Pi no roteador (reserva de DHCP).

6. [x] Mapa corrigido (estava espelhado), com setas de sentido e mapa de calor na linha: velocidade, freio, acelerador ou ganho e perda.
   - As duas voltas aparecem lado a lado, na mesma escala de cor (grossa = volta, fina = referência).
   - Rótulos "C1 +0.22" em cada curva. Tocar no rótulo amplia a curva e mostra a dica.

7. [x] Zebras pelo tipo de superfície de cada roda (`surfaceType` do jogo).
   - Mapa: as zebras de todas as voltas da pista (■) e onde a volta pôs roda fora da pista (✕).
   - Dicas: "usou / não usou a zebra na entrada, no ápice ou na saída (a referência …)" e "saiu da pista na …".
   - [ ] Conferir com voltas reais. As voltas de antes de 14/11/2026 não têm superfície.
8. [x] Bordas da pista com o TUMFTM racetrack-database: `bordas.py <trackId>` encaixa a pista real na volta do jogo e grava `bordas/<id>.json`.
   - Interlagos: escala 0,996, 97% da volta dentro das bordas e as zebras caem na beira.
   - Precisão: mais ou menos 1 m, porque a linha central foi suavizada e as larguras vêm de satélite.
   - [x] Zebras mais precisas (14/11/2026):
     - a posição e a superfície são juntadas pelo número do quadro do jogo;
     - a frente do carro vem do jogo, e não mais da trajetória;
     - os contatos de roda até 2,5 m de uma borda viram uma zebra listrada desenhada em cima dela (`RAIO_ZEBRA`).
     - Interlagos: 14 trechos, 377 m.
   - [ ] Próximo: usar as bordas nas dicas ("ápice a 0,5 m da borda de dentro"), em vez de só comparar com a referência.
9. [x] Numeração oficial das curvas (C1, C2…) pelo `pistas.json`. Só Interlagos por enquanto. Outras pistas: medir os picos de curvatura de uma volta e colocar lá.
10. [x] Redesign no estilo iOS, com 4 abas: Resumo, Mapa em tela cheia, Gráficos e Voltas.
    - A página dá para pôr na tela de início do iPhone (Compartilhar → Adicionar à Tela de Início) e abre como app.
11. [x] Modo curva a curva no mapa: ‹ ›, deslizar a folha ou as setas do teclado.
    - A folha mostra frenagem, velocidade no ápice (volta / referência) e as dicas.
    - As zebras são desenhadas em vermelho e branco, com botão para esconder.
12. [x] Status ao vivo na aba Voltas: se está recebendo, de onde, qual sessão e o rival. As voltas novas entram sozinhas na lista.
14. [x] Engenheiro com IA: botão no Resumo. O Pi roda `claude -p` com o Claude Haiku 5.5 (pela assinatura, sem chave de API) e escreve o resumo, 3 prioridades e o que foi bem. Testado no PC: 21 s.
    - [x] Claude Code instalado e logado no Pi (14/11/2026). A análise leva uns 18 s.
16. [x] Rádio do engenheiro: botão "Ligar rádio" no Resumo. Ao fim de cada volta: bipe, o tempo e a diferença na hora, e depois a dica curta do Haiku (modo rádio, uns 8 s).
    - O iPhone precisa estar com a tela ligada e a página aberta. Sem HTTPS, a página não consegue impedir a tela de apagar.
    - [ ] Testar pilotando: volume, voz pt-BR do iPhone, se a dica chega a tempo.
15. [x] Rótulos "freio", "ápice" e "saída" no lugar dos círculos e quadrados na curva ampliada.
13. [ ] Rival do Contra-Relógio: testar escolhendo um fantasma rival e conferir o status ("Rival: carro N, gravando").

## Próxima ideia: engenheiro de corrida falando

As dicas faladas como um engenheiro de F1 no rádio, ao fim de cada volta. Por exemplo: "Volta 1:10.3, meio segundo atrás da melhor. Curva 1: você está freando 10 metros cedo, pode segurar mais."
- **Jeito simples, sem instalar nada:** a página no iPhone fica aberta, consulta `/voltas` a cada poucos segundos e, quando chega uma volta nova, fala as 1 ou 2 principais dicas com a voz do navegador (`speechSynthesis`, que tem voz pt-BR no iOS).
- O texto vira "fala de rádio" com frases curtas e no imperativo, sem números demais. Pode ter variações para não ficar repetitivo.
- **Cuidado não óbvio:** o iOS só deixa a página falar depois de um toque. Então precisa de um botão "Ligar rádio" antes de começar, e a tela do iPhone tem que ficar ligada.
- **Depois:** falar durante a volta, antes da curva ("freie mais tarde aqui"), usando a posição ao vivo. Isso exige o delta ao vivo.

## Depois, se fizer falta

- **Delta ao vivo** durante a volta.
- **Curvas de pé no fundo:** segmentar a volta pela posição na pista.
- **Bordas da pista:** estimar pela dispersão de muitas voltas.
