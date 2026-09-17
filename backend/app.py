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
4. Um arquivo corrompido no meio do lote não derruba mais os demais — vira
   um `.txt` de erro dentro do próprio ZIP.
5. Proteção contra "bomba de descompressão" (imagem com dimensões
   absurdas).
6. Processamento roda em threadpool (`run_in_threadpool`) — o servidor
   continua respondendo outras requisições durante o processamento.
7. Lote vazio ou com extensão não suportada é rejeitado com uma mensagem
   clara em vez de um erro genérico.
8. Removido o redimensionamento fixo para os formatos do Instagram
   (feed/stories/quadrado): o arquivo agora sai com o mesmo formato e as
   mesmas dimensões em que foi enviado — só os metadados são removidos.
9. Adicionado suporte a vídeo (.mp4/.mov): os metadados (GPS, data/hora,
   modelo do aparelho) são removidos com FFmpeg, copiando os fluxos de
   áudio/vídeo sem recodificar — rápido e sem perda de qualidade.
10. CORS não expunha o cabeçalho `Content-Disposition` na resposta — o
    navegador o recebia, mas o `fetch()` do frontend não conseguia lê-lo
    (restrição padrão do CORS). Isso não dava pra perceber antes porque o
    nome de saída era sempre `limpa_{nome}.jpg`, fácil de "adivinhar" sem
    o cabeçalho; agora que o formato de saída varia (jpg/png/webp/mp4/mov),
    o nome errado ficaria visível. Corrigido com `expose_headers`.

Requisitos: fastapi, uvicorn, python-multipart, Pillow, e o binário
`ffmpeg` instalado no servidor (ver Dockerfile).
"""

from __future__ import annotations

import io
import os
import zipfile

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse

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

MAX_FILES = 10
MAX_BYTES = 300 * 1024 * 1024  # 300 MB por lote (vídeos são bem maiores que fotos)

MEDIA_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
}


def nome_arquivo_limpo(nome_original: str | None) -> str:
    """Gera o nome de saída preservando a extensão original (sem
    duplicá-la e sem trocar o formato do arquivo)."""
    base, ext = os.path.splitext(nome_original or "arquivo")
    return f"limpa_{base}{ext.lower()}"


def _extensao(nome_original: str | None) -> str:
    return os.path.splitext(nome_original or "")[1].lstrip(".").lower()


def _extensao_valida(nome_original: str | None) -> bool:
    return bool(nome_original) and _extensao(nome_original) in EXTENSOES_VALIDAS


@app.get("/")
async def raiz():
    return {
        "servico": "Metadata Clean API",
        "descricao": "Remove metadados (EXIF/GPS/câmera/data) de fotos e vídeos, mantendo formato e dimensões originais.",
        "endpoint": "POST /limpar",
        "extensoes_aceitas": list(EXTENSOES_VALIDAS),
        "limites": {"max_arquivos": MAX_FILES, "max_bytes_por_lote": MAX_BYTES},
    }


@app.post("/limpar")
async def limpar_arquivos(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="Nenhum arquivo enviado.")

    # 1. Validação de quantidade
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"O limite é de no máximo {MAX_FILES} arquivos por lote.")

    # 2. Leitura com checagem de tamanho acumulado (mais confiável do que
    #    confiar apenas em UploadFile.size, que pode não estar disponível
    #    em todas as versões do Starlette).
    conteudos: list[tuple[UploadFile, bytes]] = []
    tamanho_total = 0
    for f in files:
        dados = await f.read()
        tamanho_total += len(dados)
        if tamanho_total > MAX_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"O lote excede o tamanho máximo de {MAX_BYTES // (1024 * 1024)} MB.",
            )
        conteudos.append((f, dados))

    # 3. Caso de um único arquivo: processa e retorna direto.
    if len(conteudos) == 1:
        f, dados = conteudos[0]
        if not _extensao_valida(f.filename):
            raise HTTPException(
                status_code=400,
                detail=f"Extensão não suportada para '{f.filename}'. Use: {', '.join(EXTENSOES_VALIDAS)}.",
            )
        ext = _extensao(f.filename)
        try:
            arquivo_limpo, _tipo = await run_in_threadpool(processar_arquivo, dados, ext)
        except ArquivoInvalidoError as e:
            raise HTTPException(status_code=400, detail=f"'{f.filename}' não pôde ser processado: {e}")
        except ArquivoMuitoGrandeError:
            raise HTTPException(
                status_code=400,
                detail=f"'{f.filename}' excede o limite seguro de pixels (possível arquivo corrompido).",
            )

        return Response(
            content=arquivo_limpo,
            media_type=MEDIA_TYPES.get(ext, "application/octet-stream"),
            headers={"Content-Disposition": f"attachment; filename={nome_arquivo_limpo(f.filename)}"},
        )

    # 4. Vários arquivos: processa cada um individualmente. Um arquivo
    #    inválido não derruba os demais — entra como um .txt de erro
    #    dentro do próprio ZIP, e o processamento continua.
    sucessos = 0
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for f, dados in conteudos:
            nome_saida = nome_arquivo_limpo(f.filename)

            if not _extensao_valida(f.filename):
                zip_file.writestr(
                    f"ERRO_{f.filename or 'arquivo'}.txt",
                    f"Extensão não suportada. Use: {', '.join(EXTENSOES_VALIDAS)}.",
                )
                continue

            ext = _extensao(f.filename)
            try:
                arquivo_limpo, _tipo = await run_in_threadpool(processar_arquivo, dados, ext)
                zip_file.writestr(nome_saida, arquivo_limpo)
                sucessos += 1
            except ArquivoInvalidoError as e:
                zip_file.writestr(f"ERRO_{f.filename or 'arquivo'}.txt", f"Não pôde ser processado: {e}")
            except ArquivoMuitoGrandeError:
                zip_file.writestr(
                    f"ERRO_{f.filename or 'arquivo'}.txt",
                    "Imagem excede o limite seguro de pixels (possível arquivo corrompido).",
                )

    if sucessos == 0:
        raise HTTPException(status_code=400, detail="Nenhum dos arquivos enviados pôde ser processado.")

    zip_buffer.seek(0)
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=arquivos_limpos.zip"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
