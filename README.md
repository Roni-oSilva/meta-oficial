# Limpador de Metadados para Instagram

Ferramenta que remove 100% dos metadados (EXIF, GPS, modelo do aparelho,
data/hora, perfil de cor) de **fotos e vídeos**, mantendo o mesmo formato,
as mesmas dimensões, a mesma qualidade e o **mesmo nome** do arquivo
original — sem cortar, redimensionar, converter ou compactar.

## Estrutura do projeto

```
projeto/
├── frontend/
│   └── index.html              # a tela (site) — vai pro Vercel
└── backend/
    ├── app.py                  # a API — vai pro Render
    ├── nucleo_processamento.py # limpeza de fotos (Pillow) e vídeos (FFmpeg)
    ├── Dockerfile               # necessário: instala o FFmpeg no servidor
    ├── requirements.txt
    ├── test_app.py
    └── test_nucleo.py
```

Frontend e backend ficam em pastas separadas de propósito: se o Vercel
enxergar um `app.py` na mesma pasta que o `index.html`, ele tenta rodar
como um projeto Python e passa a responder no lugar do site estático.
Com a separação, cada serviço só enxerga a sua própria pasta.

## O que mudou nesta versão

- **Nunca mais gera `.zip`**: antes, selecionar 3+ arquivos compactava
  tudo num único `.zip`. Agora cada arquivo é enviado e devolvido numa
  requisição própria — **1 arquivo selecionado = 1 download**; **vários
  arquivos = vários downloads individuais**, um atrás do outro (com um
  pequeno intervalo entre eles pra o navegador não bloquear downloads
  simultâneos). O nome do arquivo baixado é **exatamente** o nome
  original enviado (sem prefixo `limpa_`, sem trocar extensão).
- **Limite de 300 MB por arquivo** (não mais somado entre arquivos do
  mesmo envio). Pode enviar, por exemplo, 3 vídeos de 250 MB, 180 MB e
  300 MB ao mesmo tempo — cada um é validado individualmente, tanto no
  navegador quanto no servidor. Um arquivo maior que isso é rejeitado
  com a mensagem "Arquivo muito grande. O tamanho máximo permitido é
  300 MB." antes mesmo de começar a subir.
- **Fila de upload de verdade**: dá pra selecionar/arrastar vários
  arquivos de uma vez, ver o progresso de cada um individualmente
  (`Pronto para enviar` → `Enviando… 45%` → `Concluído ✓` ou `Erro —
  tentar novamente`), cancelar um envio em andamento, remover um arquivo
  da lista antes de enviar, ou tentar de novo só aquele que falhou — sem
  precisar reenviar os outros. Até 3 arquivos são enviados ao mesmo
  tempo (o resto espera a vez), pra não sobrecarregar o navegador nem o
  servidor.
- **Visual totalmente refeito**: fundo com pequenos círculos orbitando
  (efeito tecnológico discreto, em `<canvas>`, leve e sem bibliotecas
  externas), painel com efeito de vidro fosco (glassmorphism), barra de
  progresso por arquivo, e microanimações mais suaves. Sem mascote, sem
  seletor de formato de saída — o arquivo sai exatamente como foi
  enviado. Sem qualquer biblioteca de UI (React/Tailwind não são usados
  aqui: é HTML/CSS/JS puro, pra continuar rodando como um único arquivo
  estático no Vercel sem precisar de etapa de build).
- **Leitura em blocos no backend**: o servidor lê o arquivo em pedaços
  de 1 MB e aborta assim que ultrapassa 300 MB, em vez de carregar um
  arquivo gigante inteiro na memória só pra descobrir depois que ele é
  grande demais.
- **Bug de CORS corrigido**: o backend não expunha o cabeçalho
  `Content-Disposition` (que carrega o nome do arquivo) para o
  JavaScript do site — corrigido com `expose_headers`.

## FFmpeg e o Dockerfile — importante

O suporte a vídeo precisa do binário `ffmpeg` instalado no servidor, e o
plano padrão (Python nativo) do Render **não permite instalar pacotes de
sistema**. Por isso o backend inclui um `Dockerfile`, que instala o
FFmpeg antes de rodar a API.

**Se você já tem o serviço `meta-dados` criado no Render como ambiente
Python**: o tipo de ambiente (Python vs Docker) não dá pra trocar depois
de criado. O caminho mais simples é:

1. Suba a pasta `backend/` (com o `Dockerfile`) pro mesmo repositório no
   GitHub.
2. No Render, crie um **novo** Web Service apontando pro mesmo
   repositório, com:
   - **Environment**: Docker
   - **Root Directory**: `backend`
   - **Dockerfile Path**: `Dockerfile`
   - **Docker Build Context Directory**: `.`

   (os dois últimos campos são relativos ao Root Directory — cole só o
   caminho mesmo, nunca comandos de shell ou código neles). Não precisa
   preencher Build/Start Command, o Render usa o `Dockerfile`
   automaticamente.
3. Quando o novo serviço estiver no ar e testado, apague o serviço antigo
   (Python) e, se quiser manter a mesma URL `meta-dados.onrender.com`,
   renomeie o serviço novo para `meta-dados` (o nome do serviço vira o
   subdomínio) — o Render libera o nome assim que o antigo é apagado.
4. Se preferir não mexer no nome, é só atualizar o campo "Endereço da
   API" no `frontend/index.html` para a URL do novo serviço.

## Rodando local (VS Code), pra testar antes de subir

```bash
cd backend
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
python app.py
```

Isso roda a API sem Docker (funciona para fotos; para testar vídeo local
também, seu computador precisa ter o `ffmpeg` instalado e no PATH — no
Windows, baixe em ffmpeg.org e adicione a pasta `bin` às variáveis de
ambiente, ou instale com `winget install ffmpeg`).

O backend sobe em `http://127.0.0.1:8000`. Abra `frontend/index.html`
no navegador e troque o "Endereço da API" para `http://127.0.0.1:8000`.

Testes automatizados (de dentro de `backend/`):
```bash
python test_app.py
python test_nucleo.py
```

## Subir pro GitHub

```bash
git init
echo "venv/
__pycache__/
*.pyc" > .gitignore
git add .
git commit -m "Downloads individuais sem zip, limite de 300MB por arquivo, visual novo"
git branch -M main
git remote add origin https://github.com/SEU-USUARIO/metadata-clean-api.git
git push -u origin main
```

(Se o repositório já existir, é só `git add`, `git commit` e `git push`
normalmente — sem precisar do `init`/`remote add` de novo.)

## Deploy do backend → Render (Docker)

1. **New → Web Service** → conecte o repositório.
2. **Root Directory**: `backend`
3. **Environment**: Docker (o Render detecta o `Dockerfile` sozinho —
   deixe Build/Start Command em branco; Dockerfile Path = `Dockerfile`,
   Docker Build Context Directory = `.`).
4. Crie o serviço e espere o build terminar (a primeira vez demora um
   pouco mais, porque baixa e instala o FFmpeg).
5. Anote a URL gerada, tipo `https://meta-dados.onrender.com`.

> No plano gratuito, o serviço "dorme" depois de um tempo sem uso e
> demora alguns segundos pra acordar na primeira requisição seguinte.
> Pra ficar sempre ativo sem esse delay é preciso um plano pago (a partir
> de ~US$7/mês).

## Deploy do frontend → Vercel

1. **Add New → Project** → importe o mesmo repositório.
2. **Root Directory**: `frontend`
3. Framework Preset: **Other**.
4. Deploy.

O `index.html` já vem com o endereço `https://meta-dados.onrender.com`
preenchido por padrão — se a URL do seu backend for diferente, troque no
próprio campo "Endereço da API" da página (edição feita ali fica só no
navegador de quem usa; pra mudar o padrão pra todo mundo, edite o
`value` do campo `apiUrl` e do link de documentação no `index.html`).

## Travar o CORS (recomendado, depois que as duas URLs finais existirem)

Em `backend/app.py`:

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://seu-projeto.vercel.app"],  # em vez de ["*"]
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)
```

## Endpoints da API

- `GET /` — informações do serviço (extensões aceitas, limite por
  arquivo).
- `POST /limpar` — envie **um único arquivo** no campo `file`
  (`multipart/form-data`). A resposta é o próprio arquivo, já limpo, com
  o mesmo nome e a mesma extensão. Para vários arquivos, o frontend faz
  uma chamada por arquivo — não existe mais um endpoint que devolve um
  `.zip`.

```bash
curl -X POST "https://SEU-BACKEND/limpar" \
  -F "file=@foto.jpg" \
  -o foto.jpg

curl -X POST "https://SEU-BACKEND/limpar" \
  -F "file=@video.mp4" \
  -o video.mp4
```

**Limites:** até 300 MB por arquivo (avaliado individualmente — não
soma entre arquivos de envios diferentes). Extensões aceitas: `.jpg`,
`.jpeg`, `.png`, `.webp`, `.mp4`, `.mov`. O frontend ainda limita a 10
arquivos por seleção, só como um teto razoável de fila — não é um
limite da API em si.

## Limitações a ter em mente

- A limpeza de vídeo remove os metadados expostos pelo container
  (GPS, data de criação, modelo do aparelho, software) — é o que cobre o
  caso comum de vídeo gravado no celular. Não é uma ferramenta forense; um
  vídeo com uma trilha de telemetria separada (ex: câmeras de ação tipo
  GoPro) pode manter dados nessa trilha específica.
- O plano gratuito do Render tem RAM limitada (512 MB); vídeos muito
  grandes ou muitos uploads simultâneos podem esbarrar nesse limite —
  copiar o vídeo (sem recodificar) é uma operação leve, mas o arquivo
  inteiro passa pela memória do processo depois de lido.
- Downloads automáticos de vários arquivos seguidos podem, em alguns
  navegadores mais restritivos, exigir que o usuário permita "downloads
  múltiplos" para o site — quando isso acontece, o site avisa e cada
  arquivo continua disponível pelo botão de baixar individual ao lado
  dele, sem precisar reprocessar nada.
