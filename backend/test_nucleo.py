"""Testes automatizados do núcleo de processamento (sem depender do
FastAPI) — cobrem remoção de metadados, rotação EXIF, transparência
preservada, formato/dimensões mantidos como enviados, arquivo inválido,
proteção contra 'bomba de descompressão', e limpeza de metadados de
vídeo com FFmpeg."""

import io
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import nucleo_processamento as nucleo

falhas = []


def checar(condicao, mensagem):
    status = "PASSOU" if condicao else "FALHOU"
    print(f"  [{status}] {mensagem}")
    if not condicao:
        falhas.append(mensagem)


def imagem_para_bytes(img: Image.Image, formato="JPEG", **kwargs) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, format=formato, **kwargs)
    return buffer.getvalue()


def teste_1_remove_metadados_e_corrige_rotacao():
    print("\n[Teste 1] Remoção de metadados + correção de rotação EXIF (JPEG)")
    img = Image.new("RGB", (2000, 1200), color=(200, 60, 60))
    exif = img.getexif()
    exif[274] = 6  # Orientation = celular gravado deitado, deve virar vertical
    dados = imagem_para_bytes(img, exif=exif)

    saida_bytes = nucleo.processar_imagem(dados, "jpg")
    with Image.open(io.BytesIO(saida_bytes)) as saida:
        checar(saida.size == (1200, 2000), f"rotação corrigida (obtido: {saida.size})")
        checar(not saida.getexif(), "EXIF vazio após limpeza")
        # o Pillow sempre grava um cabecalho JFIF tecnico (versao/unidade de
        # densidade) ao salvar qualquer JPEG - isso nao identifica ninguem.
        # o que importa e nao sobrar EXIF, GPS ou perfil de cor (ICC) do
        # arquivo original.
        checar("exif" not in saida.info, "sem bloco EXIF residual")
        checar("icc_profile" not in saida.info, "sem perfil de cor (ICC) residual")


def teste_2_mantem_formato_e_dimensoes_originais():
    print("\n[Teste 2] Não redimensiona: sai no mesmo formato e tamanho enviados")
    img = Image.new("RGB", (2000, 500), color=(5, 5, 5))  # bem panorâmica, fora de qualquer proporção do Instagram
    dados = imagem_para_bytes(img)

    saida_bytes = nucleo.processar_imagem(dados, "jpg")
    with Image.open(io.BytesIO(saida_bytes)) as saida:
        checar(saida.size == (2000, 500), f"dimensões preservadas (obtido: {saida.size})")
        checar(saida.format == "JPEG", f"formato preservado (obtido: {saida.format})")


def teste_3_png_com_transparencia_preserva_alfa():
    print("\n[Teste 3] PNG com transparência preserva o canal alfa (não vira fundo branco)")
    img = Image.new("RGBA", (800, 800), color=(0, 150, 0, 0))
    for x in range(200, 600):
        for y in range(200, 600):
            img.putpixel((x, y), (0, 150, 0, 255))
    dados = imagem_para_bytes(img, formato="PNG")

    saida_bytes = nucleo.processar_imagem(dados, "png")
    with Image.open(io.BytesIO(saida_bytes)) as saida:
        checar(saida.format == "PNG", "saiu como PNG (mesmo formato enviado)")
        checar(saida.mode == "RGBA", "canal alfa preservado")
        checar(saida.getpixel((5, 5))[3] == 0, "área transparente continua transparente")
        checar(saida.getpixel((400, 400))[:3] == (0, 150, 0), "conteúdo opaco preservado")


def teste_4_arquivo_nao_e_imagem_valida():
    print("\n[Teste 4] Arquivo que não é imagem válida gera erro tratável")
    dados = b"isso claramente nao e uma imagem valida"
    try:
        nucleo.processar_imagem(dados, "jpg")
        checar(False, "deveria ter levantado ArquivoInvalidoError")
    except nucleo.ArquivoInvalidoError:
        checar(True, "ArquivoInvalidoError levantado corretamente")
    except Exception as e:
        checar(False, f"levantou exceção inesperada: {type(e).__name__}: {e}")


def teste_5_bomba_de_descompressao_e_barrada():
    print("\n[Teste 5] Proteção contra 'bomba de descompressão'")
    img = Image.new("RGB", (500, 500), color=(10, 10, 10))
    dados = imagem_para_bytes(img)

    limite_original = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = 1000  # força a detecção numa imagem normal
        try:
            nucleo.processar_imagem(dados, "jpg")
            checar(False, "deveria ter levantado ArquivoMuitoGrandeError")
        except nucleo.ArquivoMuitoGrandeError:
            checar(True, "ArquivoMuitoGrandeError levantado corretamente")
    finally:
        Image.MAX_IMAGE_PIXELS = limite_original


def teste_6_processa_webp():
    print("\n[Teste 6] Aceita WEBP e mantém o formato")
    img = Image.new("RGB", (900, 900), color=(80, 40, 200))
    dados_webp = imagem_para_bytes(img, formato="WEBP")

    saida_bytes = nucleo.processar_imagem(dados_webp, "webp")
    with Image.open(io.BytesIO(saida_bytes)) as saida:
        checar(saida.format == "WEBP", f"saiu como WEBP (obtido: {saida.format})")
        checar(saida.size == (900, 900), "dimensões preservadas")


def teste_7_qualidade_afeta_tamanho_do_arquivo():
    print("\n[Teste 7] Qualidade menor gera arquivo JPEG final menor")
    img = Image.new("RGB", (1080, 1350), color=(120, 60, 200))
    import random
    random.seed(42)
    for _ in range(5000):
        x, y = random.randint(0, 1079), random.randint(0, 1349)
        img.putpixel((x, y), (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)))
    dados = imagem_para_bytes(img)

    saida_alta = nucleo.processar_imagem(dados, "jpg", qualidade=95)
    saida_baixa = nucleo.processar_imagem(dados, "jpg", qualidade=60)
    checar(len(saida_baixa) < len(saida_alta), f"qualidade 60 ({len(saida_baixa)}B) < qualidade 95 ({len(saida_alta)}B)")


def teste_8_tipo_do_arquivo():
    print("\n[Teste 8] Detecção de tipo (imagem/vídeo) pela extensão")
    checar(nucleo.tipo_do_arquivo("foto.jpg") == "imagem", "foto.jpg -> imagem")
    checar(nucleo.tipo_do_arquivo("foto.PNG") == "imagem", "foto.PNG -> imagem (case-insensitive)")
    checar(nucleo.tipo_do_arquivo("clipe.mp4") == "video", "clipe.mp4 -> video")
    checar(nucleo.tipo_do_arquivo("clipe.mov") == "video", "clipe.mov -> video")
    checar(nucleo.tipo_do_arquivo("documento.pdf") is None, "documento.pdf -> None (não suportado)")


def _ffmpeg_disponivel() -> bool:
    return shutil.which("ffmpeg") is not None


def _criar_video_com_metadados(caminho: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=1",
            "-metadata", "creation_time=2024-05-01T10:00:00Z",
            "-metadata", "location=-23.5505+046.6333/",
            "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart",
            str(caminho),
        ],
        check=True,
        capture_output=True,
    )


def _ler_metadados(caminho_bytes: bytes, sufixo: str) -> str:
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=sufixo) as tmp:
        tmp.write(caminho_bytes)
        tmp.flush()
        resultado = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format_tags", "-of", "default=noprint_wrappers=1", tmp.name],
            capture_output=True,
            text=True,
        )
        return resultado.stdout


def teste_9_video_remove_metadados():
    print("\n[Teste 9] Limpeza de metadados de vídeo (.mp4) com FFmpeg")
    if not _ffmpeg_disponivel():
        print("  [PULADO] ffmpeg não está instalado neste ambiente")
        return

    import tempfile
    with tempfile.TemporaryDirectory() as pasta:
        caminho_video = Path(pasta) / "video_com_metadados.mp4"
        _criar_video_com_metadados(caminho_video)
        dados_originais = caminho_video.read_bytes()

        metadados_antes = _ler_metadados(dados_originais, ".mp4")
        checar("location" in metadados_antes, "vídeo de teste tem GPS gravado antes da limpeza")

        saida_bytes = nucleo.processar_video(dados_originais, "mp4")
        metadados_depois = _ler_metadados(saida_bytes, ".mp4")
        checar("location" not in metadados_depois, "GPS removido depois da limpeza")
        checar("creation_time" not in metadados_depois, "data de criação removida depois da limpeza")
        checar(len(saida_bytes) > 0, "vídeo limpo tem conteúdo (não ficou vazio)")


def teste_10_video_invalido_gera_erro_tratavel():
    print("\n[Teste 10] Vídeo inválido gera erro tratável, não uma exceção crua")
    if not _ffmpeg_disponivel():
        print("  [PULADO] ffmpeg não está instalado neste ambiente")
        return
    dados = b"isso claramente nao e um video valido"
    try:
        nucleo.processar_video(dados, "mp4")
        checar(False, "deveria ter levantado ArquivoInvalidoError")
    except nucleo.ArquivoInvalidoError:
        checar(True, "ArquivoInvalidoError levantado corretamente")
    except Exception as e:
        checar(False, f"levantou exceção inesperada: {type(e).__name__}: {e}")


def teste_11_processar_arquivo_roteia_certo():
    print("\n[Teste 11] processar_arquivo() roteia foto -> Pillow e vídeo -> FFmpeg")
    img = Image.new("RGB", (400, 400), color=(1, 2, 3))
    dados_img = imagem_para_bytes(img)
    saida, tipo = nucleo.processar_arquivo(dados_img, "jpg")
    checar(tipo == "imagem", "extensão .jpg roteada como imagem")
    checar(len(saida) > 0, "saída da imagem tem conteúdo")

    try:
        nucleo.processar_arquivo(b"lixo", "pdf")
        checar(False, "extensão não suportada deveria levantar erro")
    except nucleo.ArquivoInvalidoError:
        checar(True, "extensão não suportada levantou ArquivoInvalidoError")


if __name__ == "__main__":
    teste_1_remove_metadados_e_corrige_rotacao()
    teste_2_mantem_formato_e_dimensoes_originais()
    teste_3_png_com_transparencia_preserva_alfa()
    teste_4_arquivo_nao_e_imagem_valida()
    teste_5_bomba_de_descompressao_e_barrada()
    teste_6_processa_webp()
    teste_7_qualidade_afeta_tamanho_do_arquivo()
    teste_8_tipo_do_arquivo()
    teste_9_video_remove_metadados()
    teste_10_video_invalido_gera_erro_tratavel()
    teste_11_processar_arquivo_roteia_certo()

    print("\n" + "=" * 60)
    if falhas:
        print(f"RESULTADO: {len(falhas)} verificacao(oes) falharam:")
        for f in falhas:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("RESULTADO: todas as verificacoes passaram.")
        sys.exit(0)
