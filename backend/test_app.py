"""Testes de ponta a ponta da API real via TestClient (faz requisições HTTP
de verdade contra o app FastAPI) — cobre fotos (sem redimensionamento,
formato preservado) e vídeos (limpeza de metadados via FFmpeg), além dos
casos de erro tratados.

Mudança importante em relação à versão anterior: o endpoint /limpar agora
processa UM arquivo por requisição (campo 'file') e nunca gera .zip — o
nome de saída é exatamente o nome original enviado. O limite de tamanho
(300 MB) é avaliado por arquivo, não mais somado entre vários arquivos de
um mesmo lote (que não existe mais)."""

import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import app as app_module
from app import app

client = TestClient(app)
falhas = []


def checar(condicao, mensagem):
    status = "PASSOU" if condicao else "FALHOU"
    print(f"  [{status}] {mensagem}")
    if not condicao:
        falhas.append(mensagem)


def imagem_bytes(tamanho=(1600, 1200), cor=(30, 90, 180), formato="JPEG", **kwargs) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", tamanho, color=cor).save(buffer, format=formato, **kwargs)
    return buffer.getvalue()


def imagem_com_exif_rotacionado_bytes() -> bytes:
    img = Image.new("RGB", (2000, 1200), color=(200, 60, 60))
    exif = img.getexif()
    exif[274] = 6  # celular gravado deitado, deveria virar vertical
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def imagem_rgba_transparente_bytes() -> bytes:
    img = Image.new("RGBA", (800, 800), color=(0, 150, 0, 0))
    for x in range(200, 600):
        for y in range(200, 600):
            img.putpixel((x, y), (0, 150, 0, 255))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def _ffmpeg_disponivel() -> bool:
    return shutil.which("ffmpeg") is not None


def video_com_metadados_bytes() -> bytes:
    with tempfile.TemporaryDirectory() as pasta:
        caminho = os.path.join(pasta, "video.mp4")
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
                "-f", "lavfi", "-i", "sine=frequency=1000:duration=1",
                "-metadata", "creation_time=2024-05-01T10:00:00Z",
                "-metadata", "location=-23.5505+046.6333/",
                "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart",
                caminho,
            ],
            check=True,
            capture_output=True,
        )
        with open(caminho, "rb") as f:
            return f.read()


def _tem_metadado(dados_video: bytes, chave: str) -> bool:
    with tempfile.NamedTemporaryFile(suffix=".mp4") as tmp:
        tmp.write(dados_video)
        tmp.flush()
        resultado = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format_tags", "-of", "default=noprint_wrappers=1", tmp.name],
            capture_output=True, text=True,
        )
        return chave in resultado.stdout


def teste_1_raiz_responde():
    print("\n[Teste 1] Endpoint raiz responde com informações do serviço")
    resp = client.get("/")
    checar(resp.status_code == 200, f"status 200 (obtido: {resp.status_code})")
    dados = resp.json()
    checar(
        set(dados.get("extensoes_aceitas", [])) == {"jpg", "jpeg", "png", "webp", "mp4", "mov"},
        f"extensões aceitas corretas (obtido: {dados.get('extensoes_aceitas')})",
    )
    checar("formatos" not in dados, "não expõe mais 'formatos' fixos do Instagram (removidos)")
    checar(
        dados.get("limites", {}).get("max_bytes_por_arquivo") == app_module.MAX_BYTES_POR_ARQUIVO,
        "informa o limite por arquivo (não mais por lote)",
    )


def teste_2_arquivo_unico_mantem_formato_e_dimensoes():
    print("\n[Teste 2] Um arquivo é limpo SEM redimensionar (mesmo formato/tamanho enviados)")
    resp = client.post(
        "/limpar",
        files={"file": ("foto.jpg", imagem_bytes(tamanho=(1600, 1200)), "image/jpeg")},
    )
    checar(resp.status_code == 200, f"status 200 (obtido: {resp.status_code}, corpo: {resp.text[:200]})")
    checar(resp.headers["content-type"] == "image/jpeg", "content-type correto")

    disposicao = resp.headers.get("content-disposition", "")
    checar(
        'filename="foto.jpg"' in disposicao,
        f"nome de saída é EXATAMENTE o original, sem prefixo 'limpa_' (obtido: {disposicao})",
    )

    with Image.open(io.BytesIO(resp.content)) as img:
        checar(img.size == (1600, 1200), f"dimensões preservadas, sem redimensionar (obtido: {img.size})")
        checar(not img.getexif(), "sem EXIF na imagem devolvida")


def teste_3_nome_de_arquivo_preserva_extensao_original():
    print("\n[Teste 3] Extensão original preservada no nome e no conteúdo (PNG, WEBP)")
    for nome_original, formato_pil, content_type in [
        ("logo.png", "PNG", "image/png"),
        ("banner.webp", "WEBP", "image/webp"),
    ]:
        resp = client.post(
            "/limpar",
            files={"file": (nome_original, imagem_bytes(formato=formato_pil), content_type)},
        )
        checar(resp.status_code == 200, f"{nome_original}: status 200 (obtido: {resp.status_code})")
        checar(resp.headers["content-type"] == content_type, f"{nome_original}: content-type é {content_type}")
        disposicao = resp.headers.get("content-disposition", "")
        esperado = f'filename="{nome_original}"'
        checar(esperado in disposicao, f"{nome_original} -> nome esperado '{esperado}' presente (obtido: {disposicao})")
        with Image.open(io.BytesIO(resp.content)) as img:
            checar(img.format == formato_pil, f"{nome_original} -> saiu como {formato_pil} de verdade (obtido: {img.format})")


def teste_4_rotacao_exif_corrigida():
    print("\n[Teste 4] Foto com EXIF de rotação sai corrigida (não deitada)")
    resp = client.post(
        "/limpar",
        files={"file": ("celular.jpg", imagem_com_exif_rotacionado_bytes(), "image/jpeg")},
    )
    checar(resp.status_code == 200, f"status 200 (obtido: {resp.status_code})")
    with Image.open(io.BytesIO(resp.content)) as img:
        checar(img.size == (1200, 2000), f"orientação corrigida (obtido: {img.size})")
        checar(not img.getexif(), "sem EXIF residual")


def teste_5_png_transparente_preserva_alfa():
    print("\n[Teste 5] PNG com transparência preserva o canal alfa (não vira fundo branco)")
    resp = client.post(
        "/limpar",
        files={"file": ("logo.png", imagem_rgba_transparente_bytes(), "image/png")},
    )
    checar(resp.status_code == 200, f"status 200 (obtido: {resp.status_code})")
    with Image.open(io.BytesIO(resp.content)) as img:
        checar(img.mode == "RGBA", f"imagem final mantém transparência (obtido: {img.mode})")
        checar(img.getpixel((5, 5))[3] == 0, "área antes transparente continua transparente")


def teste_6_varios_arquivos_nunca_geram_zip():
    print("\n[Teste 6] Enviar vários arquivos = várias requisições individuais, nunca um .zip")
    respostas = []
    for nome, cor in [("foto1.jpg", (200, 50, 50)), ("foto2.jpg", (50, 200, 50))]:
        resp = client.post("/limpar", files={"file": (nome, imagem_bytes(cor=cor), "image/jpeg")})
        respostas.append((nome, resp))

    for nome, resp in respostas:
        checar(resp.status_code == 200, f"{nome}: status 200 (obtido: {resp.status_code})")
        checar(resp.headers["content-type"] != "application/zip", f"{nome}: nunca é application/zip")
        checar(resp.headers["content-type"] == "image/jpeg", f"{nome}: content-type é a imagem, não um pacote")
        disposicao = resp.headers.get("content-disposition", "")
        checar(f'filename="{nome}"' in disposicao, f"{nome}: nome de saída bate com o original (obtido: {disposicao})")
        with Image.open(io.BytesIO(resp.content)) as img:
            checar(img.size == (1600, 1200), f"{nome}: dimensões preservadas (obtido: {img.size})")


def teste_7_arquivo_unico_corrompido_retorna_erro_claro():
    print("\n[Teste 7] Um arquivo corrompido retorna 400 claro, não 500 genérico")
    resp = client.post(
        "/limpar",
        files={"file": ("corrompida.jpg", b"lixo binario qualquer", "image/jpeg")},
    )
    checar(resp.status_code == 400, f"status 400, não 500 (obtido: {resp.status_code})")
    checar("não pôde ser processado" in resp.json().get("detail", ""), f"mensagem clara (obtido: {resp.json()})")


def teste_8_limite_de_tamanho_por_arquivo():
    print("\n[Teste 8] Arquivo acima de 300 MB é rejeitado com a mensagem exata")
    # Reduz temporariamente o limite pra nao precisar gerar 300 MB de verdade
    # so pra testar a checagem.
    limite_original = app_module.MAX_BYTES_POR_ARQUIVO
    app_module.MAX_BYTES_POR_ARQUIVO = 1 * 1024 * 1024  # 1 MB, só para este teste
    try:
        dados_grandes = os.urandom(2 * 1024 * 1024)  # 2 MB > limite do teste
        resp = client.post("/limpar", files={"file": ("grande.jpg", dados_grandes, "image/jpeg")})
        checar(resp.status_code == 400, f"status 400 (obtido: {resp.status_code})")
        checar(
            resp.json().get("detail") == "Arquivo muito grande. O tamanho máximo permitido é 300 MB.",
            f"mensagem é exatamente a esperada (obtido: {resp.json()})",
        )
    finally:
        app_module.MAX_BYTES_POR_ARQUIVO = limite_original


def teste_9_arquivo_dentro_do_limite_passa():
    print("\n[Teste 9] Arquivo abaixo do limite por arquivo passa normalmente")
    limite_original = app_module.MAX_BYTES_POR_ARQUIVO
    app_module.MAX_BYTES_POR_ARQUIVO = 5 * 1024 * 1024  # 5 MB só para este teste
    try:
        resp = client.post("/limpar", files={"file": ("ok.jpg", imagem_bytes(), "image/jpeg")})
        checar(resp.status_code == 200, f"status 200 (obtido: {resp.status_code})")
    finally:
        app_module.MAX_BYTES_POR_ARQUIVO = limite_original


def teste_10_extensao_nao_suportada_e_rejeitada():
    print("\n[Teste 10] Extensão de arquivo não suportada é rejeitada com mensagem clara")
    resp = client.post(
        "/limpar",
        files={"file": ("documento.txt", b"conteudo de texto qualquer", "text/plain")},
    )
    checar(resp.status_code == 400, f"status 400 (obtido: {resp.status_code})")
    checar("xtens" in resp.json().get("detail", ""), f"mensagem menciona extensão (obtido: {resp.json()})")


def teste_11_nenhum_arquivo_enviado():
    print("\n[Teste 11] Requisição sem nenhum arquivo é rejeitada com clareza, não com 500")
    resp = client.post("/limpar")
    checar(resp.status_code in (400, 422), f"rejeitado com um erro claro, não 500 (obtido: {resp.status_code})")


def teste_12_video_tem_metadados_removidos_e_nome_preservado():
    print("\n[Teste 12] Vídeo (.mov) é aceito, sai sem GPS/data de criação, com o nome original")
    if not _ffmpeg_disponivel():
        print("  [PULADO] ffmpeg não está instalado neste ambiente")
        return
    dados_video = video_com_metadados_bytes()
    checar(_tem_metadado(dados_video, "location"), "vídeo de teste tem GPS antes de subir (sanity check)")

    resp = client.post("/limpar", files={"file": ("clipe.mov", dados_video, "video/quicktime")})
    checar(resp.status_code == 200, f"status 200 (obtido: {resp.status_code}, corpo: {resp.text[:200]})")
    checar(resp.headers["content-type"] == "video/quicktime", f"content-type correto (obtido: {resp.headers.get('content-type')})")
    disposicao = resp.headers.get("content-disposition", "")
    checar('filename="clipe.mov"' in disposicao, f"nome de saída é exatamente 'clipe.mov' (obtido: {disposicao})")
    checar(not _tem_metadado(resp.content, "location"), "GPS removido do vídeo devolvido")
    checar(not _tem_metadado(resp.content, "creation_time"), "data de criação removida do vídeo devolvido")


if __name__ == "__main__":
    teste_1_raiz_responde()
    teste_2_arquivo_unico_mantem_formato_e_dimensoes()
    teste_3_nome_de_arquivo_preserva_extensao_original()
    teste_4_rotacao_exif_corrigida()
    teste_5_png_transparente_preserva_alfa()
    teste_6_varios_arquivos_nunca_geram_zip()
    teste_7_arquivo_unico_corrompido_retorna_erro_claro()
    teste_8_limite_de_tamanho_por_arquivo()
    teste_9_arquivo_dentro_do_limite_passa()
    teste_10_extensao_nao_suportada_e_rejeitada()
    teste_11_nenhum_arquivo_enviado()
    teste_12_video_tem_metadados_removidos_e_nome_preservado()

    print("\n" + "=" * 60)
    if falhas:
        print(f"RESULTADO: {len(falhas)} verificacao(oes) falharam:")
        for f in falhas:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("RESULTADO: todas as verificacoes passaram.")
        sys.exit(0)
