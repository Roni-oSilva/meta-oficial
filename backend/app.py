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
import shutil
import tempfile

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask

from nucleo_processamento import (
    EXTENSOES_VALIDAS,
    ArquivoInvalidoError,
    ArquivoMuitoGrandeError,
    EXTENSOES_VIDEO,
    VideoMuitoLongoError,
    processar_arquivo,
    processar_video_arquivo,
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
MAX_BYTES_POR_ARQUIVO = 300 * 1024 * 1024  # 300 MB (fotos)
MAX_BYTES_VIDEO = 2 * 1024 * 1024 * 1024  # 2 GB (vídeos; duração máx. 30 min no núcleo)
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
MENSAGEM_VIDEO_GRANDE = "Vídeo muito grande. O tamanho máximo permitido é 2 GB."
MENSAGEM_VIDEO_LONGO = "Vídeo muito longo. A duração máxima permitida é 30 minutos."


class TamanhoExcedidoError(Exception):
    """Levantado quando um arquivo individual excede MAX_BYTES_POR_ARQUIVO."""


def _extensao(nome_original: str | None) -> str:
    return os.path.splitext(nome_original or "")[1].lstrip(".").lower()


def _extensao_valida(nome_original: str | None) -> bool:
    return bool(nome_original) and _extensao(nome_original) in EXTENSOES_VALIDAS


async def _salvar_com_limite(arquivo: UploadFile, destino: str, limite_bytes: int) -> int:
    """Grava o upload em disco em blocos, abortando ao passar do limite.
    Usado para vídeos: nunca segura o arquivo (até 2 GB) na memória."""
    total = 0
    with open(destino, "wb") as f:
        while True:
            pedaco = await arquivo.read(TAMANHO_BLOCO_LEITURA)
            if not pedaco:
                break
            total += len(pedaco)
            if total > limite_bytes:
                raise TamanhoExcedidoError()
            f.write(pedaco)
    return total


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
        "limites": {
            "max_bytes_por_arquivo": MAX_BYTES_POR_ARQUIVO,
            "max_bytes_video": MAX_BYTES_VIDEO,
            "max_duracao_video_segundos": 30 * 60,
        },
    }


async def _limpar_video(file: UploadFile, ext: str):
    """Vídeo: upload -> disco -> ffmpeg -> disco -> FileResponse (streaming).
    A pasta temporária é apagada depois que a resposta termina de ser enviada."""
    pasta = tempfile.mkdtemp(prefix="limpeza_video_")
    entregue = False
    try:
        entrada = os.path.join(pasta, f"entrada.{ext}")
        saida = os.path.join(pasta, f"saida.{ext}")
        try:
            total = await _salvar_com_limite(file, entrada, MAX_BYTES_VIDEO)
        except TamanhoExcedidoError:
            raise HTTPException(status_code=400, detail=MENSAGEM_VIDEO_GRANDE)
        if total == 0:
            raise HTTPException(status_code=400, detail="O arquivo enviado está vazio.")
        try:
            await run_in_threadpool(processar_video_arquivo, entrada, saida)
        except VideoMuitoLongoError:
            raise HTTPException(status_code=400, detail=MENSAGEM_VIDEO_LONGO)
        except ArquivoInvalidoError as e:
            raise HTTPException(status_code=400, detail=f"'{file.filename}' não pôde ser processado: {e}")
        os.remove(entrada)  # libera disco antes de enviar a resposta
        entregue = True
        return FileResponse(
            saida,
            media_type=MEDIA_TYPES.get(ext, "application/octet-stream"),
            headers={"Content-Disposition": f'attachment; filename="{file.filename}"'},
            background=BackgroundTask(shutil.rmtree, pasta, ignore_errors=True),
        )
    finally:
        if not entregue:
            shutil.rmtree(pasta, ignore_errors=True)


def _erro_inesperado(e: Exception, nome: str) -> HTTPException:
    """Converte qualquer falha não prevista numa resposta com a causa em
    português. Precisa ser feito aqui (e não num handler global) para que a
    resposta ainda passe pelo CORS — senão o navegador só mostra 'erro de
    rede' e esconde o motivo real."""
    if isinstance(e, OSError) and e.errno == 28:  # ENOSPC
        return HTTPException(status_code=507, detail="O servidor ficou sem espaço em disco para processar este arquivo. Tente um arquivo menor ou tente novamente mais tarde.")
    if isinstance(e, MemoryError):
        return HTTPException(status_code=507, detail="O servidor ficou sem memória para processar este arquivo. Tente um arquivo menor.")
    print(f"[erro inesperado] {nome}: {type(e).__name__}: {e}", flush=True)
    return HTTPException(status_code=500, detail=f"Erro interno do servidor ao processar '{nome}' ({type(e).__name__}). Tente novamente; se continuar, avise o suporte com essa mensagem.")


@app.post("/limpar")
async def limpar_arquivo(file: UploadFile = File(...)):
    try:
        return await _limpar_arquivo(file)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — queremos devolver a causa, qualquer que seja
        raise _erro_inesperado(e, file.filename or "arquivo")


async def _limpar_arquivo(file: UploadFile):
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

    ext = _extensao(file.filename)
    if ext in EXTENSOES_VIDEO:
        return await _limpar_video(file, ext)

    try:
        dados = await _ler_com_limite(file, MAX_BYTES_POR_ARQUIVO)
    except TamanhoExcedidoError:
        raise HTTPException(status_code=400, detail=MENSAGEM_ARQUIVO_GRANDE)

    if not dados:
        raise HTTPException(status_code=400, detail="O arquivo enviado está vazio.")

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
