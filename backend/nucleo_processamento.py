"""
Núcleo de processamento de fotos e vídeos — lógica pura, sem nenhuma
dependência do FastAPI, para poder ser testada isoladamente
(test_nucleo.py) e reutilizada pela API (app.py).

Fotos: processadas 100% em memória (io.BytesIO) — nunca tocam o disco.
Vídeos: o FFmpeg exige arquivos reais de entrada/saída, então usamos uma
pasta temporária isolada (tempfile.TemporaryDirectory) só durante o
processamento; ela é sempre apagada no `finally`, esteja o resultado
certo ou a chamada tenha falhado — nada fica gravado depois que a função
termina.

Em ambos os casos: nem o arquivo original nem o arquivo limpo são
enviados a nenhum lugar além da resposta HTTP devolvida ao navegador.
"""

from __future__ import annotations

import io
import os
import subprocess
import tempfile

from PIL import Image, ImageOps, UnidentifiedImageError

# Protege contra "bombas de descompressao" (arquivos de imagem manipulados
# para ter dimensoes de pixel absurdas e estourar a memoria do servidor),
# mas ainda permite fotos legitimas bem grandes (ate ~150 megapixels —
# muito acima de qualquer foto de celular ou camera profissional comum).
Image.MAX_IMAGE_PIXELS = 150_000_000

EXTENSOES_IMAGEM = ("jpg", "jpeg", "png", "webp")
EXTENSOES_VIDEO = ("mp4", "mov")
EXTENSOES_VALIDAS = EXTENSOES_IMAGEM + EXTENSOES_VIDEO

FORMATO_PILLOW = {
    "jpg": "JPEG",
    "jpeg": "JPEG",
    "png": "PNG",
    "webp": "WEBP",
}

# Videos de ate 30 min: copiar fluxos (-c copy) e I/O-bound; 3 GB levam
# poucos minutos mesmo em disco lento. Folga generosa antes de abortar.
TIMEOUT_FFMPEG_SEGUNDOS = 900
MAX_DURACAO_VIDEO_SEGUNDOS = 30 * 60  # 30 minutos
TIMEOUT_FFPROBE_SEGUNDOS = 30


class ArquivoInvalidoError(Exception):
    """Levantado quando o arquivo enviado não é uma foto/vídeo válido, ou
    quando o FFmpeg não consegue processar o vídeo."""


class ArquivoMuitoGrandeError(Exception):
    """Levantado quando a imagem excede o limite seguro de pixels
    (possível 'bomba de descompressão')."""


class VideoMuitoLongoError(Exception):
    """Levantado quando o vídeo dura mais que MAX_DURACAO_VIDEO_SEGUNDOS."""


def tipo_do_arquivo(nome_ou_extensao: str) -> str | None:
    """Classifica um nome de arquivo (ou só a extensão) como 'imagem',
    'video', ou None se a extensão não é suportada."""
    ext = os.path.splitext(nome_ou_extensao)[1].lstrip(".") if "." in nome_ou_extensao else nome_ou_extensao
    ext = ext.lower()
    if ext in EXTENSOES_IMAGEM:
        return "imagem"
    if ext in EXTENSOES_VIDEO:
        return "video"
    return None


def processar_imagem(dados_brutos: bytes, extensao: str, qualidade: int = 95) -> bytes:
    """
    Remove 100% dos metadados (EXIF/GPS/câmera/data/perfil de cor) de uma
    foto e devolve os bytes prontos para download — no MESMO formato e nas
    MESMAS dimensões em que ela foi enviada. Não corta, não redimensiona e
    não converte para outro formato de arquivo.
    """
    extensao = extensao.lower().lstrip(".")
    formato_saida = FORMATO_PILLOW.get(extensao, "JPEG")

    try:
        with Image.open(io.BytesIO(dados_brutos)) as img:
            img.load()  # decodifica tudo agora, dentro do try/except

            # Corrige a rotação ANTES de descartar o EXIF (celulares salvam a
            # orientação real da foto nesse metadado).
            img = ImageOps.exif_transpose(img)

            tem_transparencia = img.mode in ("RGBA", "LA") or (
                img.mode == "P" and "transparency" in img.info
            )

            # PNG/WEBP suportam transparência: preservamos o canal alfa.
            # JPEG não suporta: achatamos sobre fundo branco só nesse caso.
            if formato_saida in ("PNG", "WEBP") and tem_transparencia:
                img_pronta = img.convert("RGBA")
            elif tem_transparencia:
                fundo = Image.new("RGB", img.size, (255, 255, 255))
                img_rgba = img.convert("RGBA")
                fundo.paste(img_rgba, mask=img_rgba.split()[-1])
                img_pronta = fundo
            else:
                img_pronta = img.convert("RGB")

            # Recria a imagem a partir de puros bytes de pixel: nenhum bloco
            # de metadado do arquivo original sobrevive nesse novo objeto.
            img_limpa = Image.frombytes(img_pronta.mode, img_pronta.size, img_pronta.tobytes())

            buffer = io.BytesIO()
            if formato_saida == "JPEG":
                img_limpa.save(buffer, format="JPEG", quality=qualidade, optimize=True)
            elif formato_saida == "WEBP":
                img_limpa.save(buffer, format="WEBP", quality=qualidade)
            else:
                img_limpa.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue()

    except Image.DecompressionBombError as e:
        raise ArquivoMuitoGrandeError(str(e)) from e
    except UnidentifiedImageError as e:
        raise ArquivoInvalidoError(str(e)) from e


def duracao_do_video(caminho: str) -> float | None:
    """Duração em segundos via ffprobe, ou None se não for possível ler."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", caminho],
            capture_output=True, timeout=TIMEOUT_FFPROBE_SEGUNDOS,
        )
        return float(r.stdout.decode().strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return None


def processar_video_arquivo(caminho_entrada: str, caminho_saida: str, max_duracao: float | None = MAX_DURACAO_VIDEO_SEGUNDOS) -> None:
    """
    Remove metadados do vídeo em `caminho_entrada` e grava o resultado em
    `caminho_saida`, trabalhando só com arquivos em disco (nunca carrega o
    vídeo inteiro na memória — essencial para arquivos de vários GB).
    Copia os fluxos sem recodificar (`-c copy`). Se `max_duracao` for
    informado e o vídeo for mais longo, levanta VideoMuitoLongoError.
    """
    if max_duracao is not None:
        duracao = duracao_do_video(caminho_entrada)
        if duracao is not None and duracao > max_duracao:
            raise VideoMuitoLongoError(duracao)

    comando = [
        "ffmpeg", "-y",
        "-i", caminho_entrada,
        "-map_metadata", "-1",   # descarta todos os metadados do arquivo original
        "-fflags", "+bitexact",  # não deixa o proprio ffmpeg escrever nada extra
        "-movflags", "+faststart",
        "-c", "copy",             # copia audio/video sem recodificar (rapido, sem perda)
        caminho_saida,
    ]

    try:
        resultado = subprocess.run(
            comando,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=TIMEOUT_FFMPEG_SEGUNDOS,
        )
    except FileNotFoundError as e:
        raise ArquivoInvalidoError(
            "FFmpeg não está instalado no servidor — instale-o para processar vídeos."
        ) from e
    except subprocess.TimeoutExpired as e:
        raise ArquivoInvalidoError("O vídeo demorou demais para processar (tempo esgotado).") from e

    if resultado.returncode != 0 or not os.path.exists(caminho_saida):
        mensagem = resultado.stderr.decode("utf-8", errors="ignore").strip().splitlines()
        detalhe = mensagem[-1] if mensagem else "falha desconhecida do FFmpeg."
        raise ArquivoInvalidoError(f"Não foi possível processar o vídeo: {detalhe}")


def processar_video(dados_brutos: bytes, extensao: str) -> bytes:
    """Versão em memória de `processar_video_arquivo` (bytes -> bytes),
    mantida para vídeos pequenos e para os testes. Sem limite de duração
    (o limite é aplicado pela API, que usa `processar_video_arquivo`)."""
    extensao = extensao.lower().lstrip(".")

    with tempfile.TemporaryDirectory(prefix="limpeza_video_") as pasta:
        caminho_entrada = os.path.join(pasta, f"entrada.{extensao}")
        caminho_saida = os.path.join(pasta, f"saida.{extensao}")

        with open(caminho_entrada, "wb") as f:
            f.write(dados_brutos)

        processar_video_arquivo(caminho_entrada, caminho_saida, max_duracao=None)

        with open(caminho_saida, "rb") as f:
            return f.read()


def processar_arquivo(dados_brutos: bytes, extensao: str) -> tuple[bytes, str]:
    """Detecta se é foto ou vídeo pela extensão e aplica o pipeline certo.
    Retorna (bytes_prontos, tipo), onde tipo é 'imagem' ou 'video'."""
    tipo = tipo_do_arquivo(extensao)
    if tipo == "imagem":
        return processar_imagem(dados_brutos, extensao), "imagem"
    if tipo == "video":
        return processar_video(dados_brutos, extensao), "video"
    raise ArquivoInvalidoError(f"Extensão '{extensao}' não suportada.")
