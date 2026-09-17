"""Testes de ponta a ponta da API real via TestClient (faz requisições HTTP
de verdade contra o app FastAPI) — cobre fotos (sem redimensionamento,
formato preservado) e vídeos (limpeza de metadados via FFmpeg), além dos
casos de erro tratados."""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import app as app_module
from app import MAX_FILES, app

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


def teste_2_arquivo_unico_mantem_formato_e_dimensoes():
    print("\n[Teste 2] Um único arquivo é limpo SEM redimensionar (mesmo formato/tamanho enviados)")
    resp = client.post(
        "/limpar",
        files={"files": ("foto.jpg", imagem_bytes(tamanho=(1600, 1200)), "image/jpeg")},
    )
    checar(resp.status_code == 200, f"status 200 (obtido: {resp.status_code}, corpo: {resp.text[:200]})")
    checar(resp.headers["content-type"] == "image/jpeg", "content-type correto")

    disposicao = resp.headers.get("content-disposition", "")
    checar(
        "limpa_foto.jpg" in disposicao and "limpa_foto.jpg.jpg" not in disposicao,
        f"nome de arquivo sem extensão duplicada (obtido: {disposicao})",
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
            files={"files": (nome_original, imagem_bytes(formato=formato_pil), content_type)},
        )
        checar(resp.status_code == 200, f"{nome_original}: status 200 (obtido: {resp.status_code})")
        checar(resp.headers["content-type"] == content_type, f"{nome_original}: content-type é {content_type}")
        disposicao = resp.headers.get("content-disposition", "")
        esperado = f"limpa_{nome_original}"
        checar(esperado in disposicao, f"{nome_original} -> nome esperado '{esperado}' presente (obtido: {disposicao})")
        with Image.open(io.BytesIO(resp.content)) as img:
            checar(img.format == formato_pil, f"{nome_original} -> saiu como {formato_pil} de verdade (obtido: {img.format})")


def teste_4_rotacao_exif_corrigida():
    print("\n[Teste 4] Foto com EXIF de rotação sai corrigida (não deitada)")
    resp = client.post(
        "/limpar",
        files={"files": ("celular.jpg", imagem_com_exif_rotacionado_bytes(), "image/jpeg")},
    )
    checar(resp.status_code == 200, f"status 200 (obtido: {resp.status_code})")
    with Image.open(io.BytesIO(resp.content)) as img:
        # original era 2000x1200 (paisagem) com EXIF pedindo rotação 90°;
        # corrigido, o resultado sai vertical: 1200x2000.
        checar(img.size == (1200, 2000), f"orientação corrigida (obtido: {img.size})")
        checar(not img.getexif(), "sem EXIF residual")


def teste_5_png_transparente_preserva_alfa():
    print("\n[Teste 5] PNG com transparência preserva o canal alfa (não vira fundo branco)")
    resp = client.post(
        "/limpar",
        files={"files": ("logo.png", imagem_rgba_transparente_bytes(), "image/png")},
    )
    checar(resp.status_code == 200, f"status 200 (obtido: {resp.status_code})")
    with Image.open(io.BytesIO(resp.content)) as img:
        checar(img.mode == "RGBA", f"imagem final mantém transparência (obtido: {img.mode})")
        checar(img.getpixel((5, 5))[3] == 0, "área antes transparente continua transparente")


def teste_6_multiplos_arquivos_geram_zip():
    print("\n[Teste 6] Vários arquivos válidos geram um .zip com todos dentro")
    resp = client.post(
        "/limpar",
        files=[
            ("files", ("foto1.jpg", imagem_bytes(cor=(200, 50, 50)), "image/jpeg")),
            ("files", ("foto2.jpg", imagem_bytes(cor=(50, 200, 50)), "image/jpeg")),
        ],
    )
    checar(resp.status_code == 200, f"status 200 (obtido: {resp.status_code})")
    checar(resp.headers["content-type"] == "application/zip", "content-type application/zip")

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        nomes = zf.namelist()
        checar(set(nomes) == {"limpa_foto1.jpg", "limpa_foto2.jpg"}, f"nomes corretos no zip (obtido: {nomes})")
        with Image.open(io.BytesIO(zf.read("limpa_foto1.jpg"))) as img:
            checar(img.size == (1600, 1200), f"dimensões preservadas dentro do zip (obtido: {img.size})")


def teste_7_arquivo_corrompido_no_lote_nao_derruba_os_demais():
    print("\n[Teste 7] Arquivo corrompido no meio do lote não derruba os demais")
    resp = client.post(
        "/limpar",
        files=[
            ("files", ("boa.jpg", imagem_bytes(), "image/jpeg")),
            ("files", ("corrompida.jpg", b"isso claramente nao e uma imagem", "image/jpeg")),
        ],
    )
    checar(resp.status_code == 200, f"requisição NÃO derrubada com 500 (status obtido: {resp.status_code})")
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        nomes = zf.namelist()
        checar("limpa_boa.jpg" in nomes, f"o arquivo válido ainda foi processado (obtido: {nomes})")
        checar(
            any(n.startswith("ERRO_") for n in nomes),
            f"o arquivo corrompido virou um aviso de erro dentro do zip (obtido: {nomes})",
        )


def teste_8_arquivo_unico_corrompido_retorna_erro_claro():
    print("\n[Teste 8] Um único arquivo corrompido retorna 400 claro, não 500 genérico")
    resp = client.post(
        "/limpar",
        files={"files": ("corrompida.jpg", b"lixo binario qualquer", "image/jpeg")},
    )
    checar(resp.status_code == 400, f"status 400, não 500 (obtido: {resp.status_code})")
    checar("não pôde ser processado" in resp.json().get("detail", ""), f"mensagem clara (obtido: {resp.json()})")


def teste_9_todos_os_arquivos_invalidos_retorna_400():
    print("\n[Teste 9] Lote inteiro inválido retorna 400 em vez de um zip vazio")
    resp = client.post(
        "/limpar",
        files=[
            ("files", ("a.jpg", b"lixo1", "image/jpeg")),
            ("files", ("b.jpg", b"lixo2", "image/jpeg")),
        ],
    )
    checar(resp.status_code == 400, f"status 400 (obtido: {resp.status_code})")


def teste_10_limite_de_quantidade_de_arquivos():
    print(f"\n[Teste 10] Mais de {MAX_FILES} arquivos é rejeitado")
    arquivos = [("files", (f"foto{i}.jpg", imagem_bytes(tamanho=(100, 100)), "image/jpeg")) for i in range(MAX_FILES + 1)]
    resp = client.post("/limpar", files=arquivos)
    checar(resp.status_code == 400, f"status 400 (obtido: {resp.status_code})")
    checar(str(MAX_FILES) in resp.json().get("detail", ""), "mensagem menciona o limite")


def teste_11_limite_de_tamanho_total():
    print("\n[Teste 11] Lote acima do limite de tamanho é rejeitado")
    # Reduz temporariamente o limite pra nao precisar gerar centenas de MB
    # so pra testar a checagem (o limite real e 300 MB, pensado pra video).
    limite_original = app_module.MAX_BYTES
    app_module.MAX_BYTES = 2 * 1024 * 1024  # 2 MB, só para este teste
    try:
        dados_grandes = os.urandom(1024 * 1024)  # 1 MB por arquivo
        arquivos = [("files", (f"grande{i}.jpg", dados_grandes, "image/jpeg")) for i in range(3)]
        resp = client.post("/limpar", files=arquivos)
        checar(resp.status_code == 400, f"status 400 (obtido: {resp.status_code})")
        checar("MB" in resp.json().get("detail", ""), "mensagem menciona o limite de tamanho")
    finally:
        app_module.MAX_BYTES = limite_original


def teste_12_extensao_nao_suportada_e_rejeitada():
    print("\n[Teste 12] Extensão de arquivo não suportada é rejeitada com mensagem clara")
    resp = client.post(
        "/limpar",
        files={"files": ("documento.txt", b"conteudo de texto qualquer", "text/plain")},
    )
    checar(resp.status_code == 400, f"status 400 (obtido: {resp.status_code})")
    checar("xtens" in resp.json().get("detail", ""), f"mensagem menciona extensão (obtido: {resp.json()})")


def teste_13_nenhum_arquivo_enviado():
    print("\n[Teste 13] Requisição sem nenhum arquivo é rejeitada com clareza, não com 500")
    resp = client.post("/limpar")
    checar(resp.status_code in (400, 422), f"rejeitado com um erro claro, não 500 (obtido: {resp.status_code})")


def teste_14_video_tem_metadados_removidos():
    print("\n[Teste 14] Vídeo (.mp4) é aceito e sai sem GPS/data de criação")
    if not _ffmpeg_disponivel():
        print("  [PULADO] ffmpeg não está instalado neste ambiente")
        return
    dados_video = video_com_metadados_bytes()
    checar(_tem_metadado(dados_video, "location"), "vídeo de teste tem GPS antes de subir (sanity check)")

    resp = client.post("/limpar", files={"files": ("clipe.mov", dados_video, "video/quicktime")})
    checar(resp.status_code == 200, f"status 200 (obtido: {resp.status_code}, corpo: {resp.text[:200]})")
    checar(resp.headers["content-type"] == "video/quicktime", f"content-type correto (obtido: {resp.headers.get('content-type')})")
    disposicao = resp.headers.get("content-disposition", "")
    checar("limpa_clipe.mov" in disposicao, f"nome preserva extensão .mov (obtido: {disposicao})")
    checar(not _tem_metadado(resp.content, "location"), "GPS removido do vídeo devolvido")
    checar(not _tem_metadado(resp.content, "creation_time"), "data de criação removida do vídeo devolvido")


def teste_15_lote_misto_fotos_e_video_no_mesmo_zip():
    print("\n[Teste 15] Lote misto (foto + vídeo) gera um único .zip com os dois limpos")
    if not _ffmpeg_disponivel():
        print("  [PULADO] ffmpeg não está instalado neste ambiente")
        return
    resp = client.post(
        "/limpar",
        files=[
            ("files", ("foto.jpg", imagem_bytes(), "image/jpeg")),
            ("files", ("clipe.mp4", video_com_metadados_bytes(), "video/mp4")),
        ],
    )
    checar(resp.status_code == 200, f"status 200 (obtido: {resp.status_code})")
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        nomes = zf.namelist()
        checar(set(nomes) == {"limpa_foto.jpg", "limpa_clipe.mp4"}, f"os dois tipos no mesmo zip (obtido: {nomes})")
        checar(not _tem_metadado(zf.read("limpa_clipe.mp4"), "location"), "GPS removido do vídeo dentro do zip")


if __name__ == "__main__":
    teste_1_raiz_responde()
    teste_2_arquivo_unico_mantem_formato_e_dimensoes()
    teste_3_nome_de_arquivo_preserva_extensao_original()
    teste_4_rotacao_exif_corrigida()
    teste_5_png_transparente_preserva_alfa()
    teste_6_multiplos_arquivos_geram_zip()
    teste_7_arquivo_corrompido_no_lote_nao_derruba_os_demais()
    teste_8_arquivo_unico_corrompido_retorna_erro_claro()
    teste_9_todos_os_arquivos_invalidos_retorna_400()
    teste_10_limite_de_quantidade_de_arquivos()
    teste_11_limite_de_tamanho_total()
    teste_12_extensao_nao_suportada_e_rejeitada()
    teste_13_nenhum_arquivo_enviado()
    teste_14_video_tem_metadados_removidos()
    teste_15_lote_misto_fotos_e_video_no_mesmo_zip()

    print("\n" + "=" * 60)
    if falhas:
        print(f"RESULTADO: {len(falhas)} verificacao(oes) falharam:")
        for f in falhas:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("RESULTADO: todas as verificacoes passaram.")
        sys.exit(0)
