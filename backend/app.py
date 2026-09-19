"""
Metadata Clean API — fotos e vídeos, sem redimensionamento fixo

Histórico de correções em relação à primeira versão enviada:

1. Sem correção de rotação EXIF: fotos verticais de celular saíam deitadas.
   Corrigido aplicando `ImageOps.exif_transpose` antes de descartar o EXIF.
2. PNG com transparência gerava manchas escuras (canal alfa descartado sem
   compor sobre nada). Corrigido — e depois refinado: como o arquivo agora
   sai no mesmo formato em que foi enviado, PNG/WEBP mantêm a
   transparência de verdade em vez de ganhar fundo branco.
3. Nome de arquivo com extensão duplicada (`foto.png` virava
   `limpa_foto.png.jpg`). Corrigido; agora a extensão de saída sempre
   corresponde à extensão original do arquivo.
4. Proteção contra "bomba de descompressão" (imagem com dimensões
   absurdas).
5. Processamento roda em threadpool (`run_in_threadpool`) — o servidor
   continua respondendo outras requisições durante o processamento.
6. Lote vazio ou com extensão não suportada é rejeitado com uma mensagem
   clara em vez de um erro genérico.
7. Removido o redimensionamento fixo para os formatos do Instagram
   (feed/stories/quadrado): o arquivo agora sai com o mesmo formato e as
   mesmas dimensões em que foi enviado — só os metadados são removidos.
8. Adicionado suporte a vídeo (.mp4/.mov): os metadados (GPS, data/hora,
   modelo do aparelho) são removidos com FFmpeg, copiando os fluxos de
   áudio/vídeo sem recodificar — rápido e sem perda de qualidade.
9. CORS não expunha o cabeçalho `Content-Disposition` na resposta — o
   navegador o recebia, mas o `fetch()` do frontend não conseguia lê-lo
   (restrição padrão do CORS). Corrigido com `expose_headers`.
10. Removida a compactação automática em ZIP quando vários arquivos eram
    enviados de uma vez. Agora cada arquivo é enviado ao servidor e
    devolvido individualmente, numa requisição própria — o frontend faz
    uma chamada por arquivo (com fila e progresso), nunca um `.zip`. Isso
    também elimina o prefixo "limpa_": o arquivo volta com o **nome
    original**, exatamente como foi enviado.
11. O limite de tamanho passou a ser avaliado **por arquivo** (300 MB
    cada), não mais somado entre todos os arquivos de um envio. A leitura
    do corpo da requisição agora é feita em blocos (streaming), abortando
    assim que o limite é ultrapassado — evita carregar um arquivo gigante
    inteiro na memória só para descobrir depois que ele excede o limite.

Requisitos: fastapi, uvicorn, python-multipart, Pillow, e o binário
`ffmpeg` instalado no servidor (ver Dockerfile).
"""

from __future__ import annotations

import os

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from nucleo_processamento import (
    EXTENSOES_VALIDAS,
    ArquivoInvalidoError,
    ArquivoMuitoGrandeError,
    processar_arquivo,
)

app = FastAPI(title="Metadata Clean API")

# Permite acesso a partir do frontend. Para uso em produção exposto
# publicamente, restrinja allow_origins ao domínio real do site (Vercel).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    # Sem isso, o navegador recebe o cabecalho Content-Disposition (com o
    # nome do arquivo) na resposta, mas o JavaScript do frontend nao
    # consegue le-lo via fetch() — o CORS bloqueia por padrao qualquer
    # cabecalho de resposta que nao esteja nesta lista.
    expose_headers=["Content-Disposition"],
)

# Limite avaliado por arquivo (não somado entre arquivos de um mesmo lote).
MAX_BYTES_POR_ARQUIVO = 300 * 1024 * 1024  # 300 MB
TAMANHO_BLOCO_LEITURA = 1024 * 1024  # 1 MB por vez — evita segurar o arquivo inteiro em memória sem necessidade

MEDIA_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
}

MENSAGEM_ARQUIVO_GRANDE = "Arquivo muito grande. O tamanho máximo permitido é 300 MB."


class TamanhoExcedidoError(Exception):
    """Levantado quando um arquivo individual excede MAX_BYTES_POR_ARQUIVO."""


def _extensao(nome_original: str | None) -> str:
    return os.path.splitext(nome_original or "")[1].lstrip(".").lower()


def _extensao_valida(nome_original: str | None) -> bool:
    return bool(nome_original) and _extensao(nome_original) in EXTENSOES_VALIDAS


async def _ler_com_limite(arquivo: UploadFile, limite_bytes: int) -> bytes:
    """Lê o corpo do upload em blocos, abortando assim que ultrapassar o
    limite — evita carregar um arquivo gigante inteiro na memória só para
    descartá-lo em seguida por ser grande demais."""
    pedacos: list[bytes] = []
    total = 0
    while True:
        pedaco = await arquivo.read(TAMANHO_BLOCO_LEITURA)
        if not pedaco:
            break
        total += len(pedaco)
        if total > limite_bytes:
            raise TamanhoExcedidoError()
        pedacos.append(pedaco)
    return b"".join(pedacos)


@app.get("/")
async def raiz():
    return {
        "servico": "Metadata Clean API",
        "descricao": "Remove metadados (EXIF/GPS/câmera/data) de fotos e vídeos, mantendo formato, dimensões, qualidade e nome originais.",
        "endpoint": "POST /limpar (um arquivo por requisição, campo 'file')",
        "extensoes_aceitas": list(EXTENSOES_VALIDAS),
        "limites": {"max_bytes_por_arquivo": MAX_BYTES_POR_ARQUIVO},
    }


@app.post("/limpar")
async def limpar_arquivo(file: UploadFile = File(...)):
    """
    Processa um único arquivo por requisição e devolve o arquivo limpo
    (mesmo nome, mesma extensão, mesmo formato) pronto para download.

    O frontend chama este endpoint uma vez por arquivo selecionado — nunca
    agrupa vários arquivos numa única resposta compactada (.zip).
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="Nenhum arquivo enviado.")

    if not _extensao_valida(file.filename):
        raise HTTPException(
            status_code=400,
            detail=f"Extensão não suportada para '{file.filename}'. Use: {', '.join(EXTENSOES_VALIDAS)}.",
        )

    try:
        dados = await _ler_com_limite(file, MAX_BYTES_POR_ARQUIVO)
    except TamanhoExcedidoError:
        raise HTTPException(status_code=400, detail=MENSAGEM_ARQUIVO_GRANDE)

    if not dados:
        raise HTTPException(status_code=400, detail="O arquivo enviado está vazio.")

    ext = _extensao(file.filename)
    try:
        arquivo_limpo, _tipo = await run_in_threadpool(processar_arquivo, dados, ext)
    except ArquivoInvalidoError as e:
        raise HTTPException(status_code=400, detail=f"'{file.filename}' não pôde ser processado: {e}")
    except ArquivoMuitoGrandeError:
        raise HTTPException(
            status_code=400,
            detail=f"'{file.filename}' excede o limite seguro de pixels (possível arquivo corrompido).",
        )

    # Nome de saída = nome original, exatamente como enviado (sem prefixo,
    # sem trocar extensão) — o arquivo que volta é o mesmo arquivo, só sem
    # os metadados.
    return Response(
        content=arquivo_limpo,
        media_type=MEDIA_TYPES.get(ext, "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{file.filename}"'},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
