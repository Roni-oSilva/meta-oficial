# Limpador de Metadados para Instagram

Ferramenta que remove 100% dos metadados (EXIF, GPS, modelo do aparelho,
data/hora, perfil de cor) de **fotos e vídeos**, mantendo o mesmo formato
e as mesmas dimensões do arquivo original — sem cortar, redimensionar ou
converter.

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

- **Sem redimensionamento fixo**: os formatos "Feed/Stories/Quadrado"
  foram removidos. O arquivo agora sai com o mesmo formato e as mesmas
  dimensões em que foi enviado — só os metadados são removidos.
- **Suporte a vídeo** (`.mp4`, `.mov`): os metadados (GPS, data de
  criação, modelo do aparelho) são removidos com FFmpeg, copiando os
  fluxos de áudio/vídeo sem recodificar — rápido e sem perda de
  qualidade.
- **Visual totalmente refeito**: sem o mascote/robô, com tipografia
  (Inter + JetBrains Mono), um indicador ao vivo de "API conectada" no
  topo, thumbnails reais das fotos/vídeos selecionados, e microanimações
  no lugar de qualquer personagem.
- **Bug de CORS corrigido**: o backend não expunha o cabeçalho
  `Content-Disposition` (que carrega o nome do arquivo) para o
  JavaScript do site. Antes isso não dava pra perceber porque o nome de
  saída era sempre `.jpg`; agora que o formato varia, ficaria visível.
  Corrigido com `expose_headers` no CORS.

## FFmpeg e o Dockerfile — importante

O suporte a vídeo precisa do binário `ffmpeg` instalado no servidor, e o
plano padrão (Python nativo) do Render **não permite instalar pacotes de
sistema**. Por isso o backend agora inclui um `Dockerfile`, que instala o
FFmpeg antes de rodar a API.

**Se você já tem o serviço `meta-dados` criado no Render como ambiente
Python**: o tipo de ambiente (Python vs Docker) não dá pra trocar depois
de criado. O caminho mais simples é:

1. Suba a pasta `backend/` (com o `Dockerfile` novo) pro mesmo
   repositório no GitHub.
2. No Render, crie um **novo** Web Service apontando pro mesmo
   repositório, com **Environment: Docker** e **Root Directory: backend**
   — não precisa preencher Build/Start Command, o Render usa o
   `Dockerfile` automaticamente.
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
git commit -m "Fotos e videos sem redimensionamento, visual novo, Docker com ffmpeg"
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
   deixe Build/Start Command em branco).
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

- `GET /` — informações do serviço (extensões aceitas, limites).
- `POST /limpar` — envie de 1 a 10 arquivos no campo `files`
  (`multipart/form-data`). Um único arquivo devolve o arquivo pronto
  direto; vários devolvem um `.zip` (pode misturar fotos e vídeos no
  mesmo lote).

```bash
curl -X POST "https://SEU-BACKEND/limpar" \
  -F "files=@foto.jpg" \
  -F "files=@video.mp4" \
  -o arquivos_limpos.zip
```

**Limites:** até 10 arquivos por lote, 300 MB no total (aumentado em
relação à versão só-fotos, porque vídeo pesa mais). Extensões aceitas:
`.jpg`, `.jpeg`, `.png`, `.webp`, `.mp4`, `.mov`.

## Limitações a ter em mente

- A limpeza de vídeo remove os metadados expostos pelo container
  (GPS, data de criação, modelo do aparelho, software) — é o que cobre o
  caso comum de vídeo gravado no celular. Não é uma ferramenta forense; um
  vídeo com uma trilha de telemetria separada (ex: câmeras de ação tipo
  GoPro) pode manter dados nessa trilha específica.
- O plano gratuito do Render tem RAM limitada (512 MB); vídeos muito
  grandes ou muitos uploads simultâneos podem esbarrar nesse limite —
  copiar o vídeo (sem recodificar) é uma operação leve, mas o arquivo
  inteiro passa pela memória do processo.
