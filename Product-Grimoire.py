# -*- coding: utf-8 -*-
"""
# Product-Grimoire

Este script foi criado para turbinar uma planilha de produtos (CSV ou Excel), buscando informações online para enriquecê-la.
Sabe quando você tem uma lista de produtos só com o código e o nome, e precisa cadastrá-los em algum lugar com descrição, fotos, etc?
Este script automatiza exatamente essa tarefa.

## O que ele faz?

Para cada produto na sua planilha, o script vai:
1.  **Buscar no Google:** Encontrar a página de venda mais relevante para o produto.
2.  **Capturar a Imagem:** Tentar extrair a URL da imagem principal da página. Se falhar, ele fará uma busca no Google Imagens como último recurso.
3.  **Baixar a Imagem:** Salvar a imagem em uma pasta local, usando o SKU do produto como nome do arquivo (ex: `PROD123.jpg`).
4.  **Criar Descrições com IA:** Usar a API do Google Gemini para gerar uma descrição curta (texto) e uma descrição longa (formatada em HTML).
5.  **Exportar o Resultado:** Juntar todas as informações novas em um arquivo Excel organizado, pronto para importação.

## Como Usar

1.  **Instale as dependências:**
    A nova versão usa a biblioteca `tqdm` para uma barra de progresso visual.
    ```bash
    pip install pandas requests beautifulsoup4 google-api-python-client google-generativeai openpyxl tqdm
    ```

2.  **Insira suas Chaves de API no Código:**
    Abra este arquivo de script e cole suas chaves de API nas variáveis correspondentes na seção `# --- CHAVES DE API ---`.
"""

import pandas as pd
import requests
from bs4 import BeautifulSoup
import time
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import google.generativeai as genai
import os
from urllib.parse import urlparse, urljoin
from tqdm import tqdm # Importa a biblioteca para a barra de progresso

# --- CONFIGURAÇÕES ---

# Ajuste os nomes das colunas se o seu arquivo de entrada for diferente
COL_SKU = 'CODIGO'
COL_NAME = 'DESCRICAO_CODIGO'
COL_QTY = 'QUANT.'
COL_PRICE = 'VENDA'

# --- CHAVES DE API (INSIRA AS SUAS AQUI) ---
# ATENÇÃO: Cole suas chaves de API diretamente aqui.
GOOGLE_API_KEY = "COLE_SUA_CHAVE_DA_GOOGLE_API_AQUI"
CSE_ID = "COLE_SEU_ID_DO_CUSTOM_SEARCH_ENGINE_AQUI"
GEMINI_API_KEY = "COLE_SUA_CHAVE_DA_API_GEMINI_AQUI"


# Arquivos de saída
IMAGE_FOLDER = 'imagens_produtos'
OUTPUT_FILENAME = 'produtos_formatados.xlsx'

# --- FUNÇÕES ---

def search_product_page_url(product_name, service, cse_id):
    """Usa a busca do Google para achar a página de um produto."""
    # A impressão foi removida daqui para não poluir a saída da barra de progresso
    try:
        result = service.cse().list(q=product_name, cx=cse_id, num=1).execute()
        if 'items' in result and result['items']:
            url = result['items'][0]['link']
            return url
        return None
    except HttpError as e:
        if e.resp.status == 429:
            tqdm.write("  -> 🛑 ERRO DE QUOTA: O limite diário de buscas da API do Google foi excedido.")
        else:
            tqdm.write(f"  -> Erro na API de busca do Google: {e}")
        return None
    except Exception as e:
        tqdm.write(f"  -> Ocorreu um erro inesperado na busca: {e}")
        return None

def search_google_images(product_name, service, cse_id):
    """Como último recurso, busca a imagem do produto diretamente no Google Imagens."""
    tqdm.write("  -> Tentativa final: buscando no Google Imagens...")
    try:
        result = service.cse().list(
            q=product_name,
            cx=cse_id,
            searchType='image',
            num=1
        ).execute()
        if 'items' in result and result['items']:
            image_url = result['items'][0]['link']
            tqdm.write(f"  -> Imagem encontrada via Google Imagens: {image_url}")
            return image_url
        return None
    except Exception as e:
        tqdm.write(f"  -> Erro ao buscar no Google Imagens: {e}")
        return None


def extract_image_url(page_url, product_name, service, cse_id):
    """
    Extrai a URL da imagem principal. Se falhar, busca no Google Imagens.
    """
    if not page_url:
        return search_google_images(product_name, service, cse_id)

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        resp = requests.get(page_url, headers=headers, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        # --- Tentativa 1: Seletor específico (ex: Mercado Livre) ---
        img_tag = soup.find('img', class_='ui-pdp-gallery__figure__image')
        if img_tag and img_tag.get('data-zoom'):
            return img_tag.get('data-zoom')

        # --- Tentativa 2: Meta tag 'og:image' ---
        og_tag = soup.find('meta', property='og:image')
        if og_tag and og_tag.get('content'):
            return og_tag.get('content')

        # --- Tentativa 3: Análise abrangente de todas as tags <img> ---
        all_imgs = soup.find_all('img')
        candidate_urls = []
        for img in all_imgs:
            src = img.get('data-zoom') or img.get('data-src') or img.get('src')
            if not src:
                continue
            
            src = urljoin(page_url, src)
            src_lower = src.lower()
            if any(keyword in src_lower for keyword in ['logo', 'icon', 'avatar', 'spinner', '.svg', '.gif', 'base64']):
                continue
            
            candidate_urls.append(src)

        for url in candidate_urls:
            if 'zoom' in url.lower() or 'large' in url.lower():
                return url
        
        if candidate_urls:
            return candidate_urls[0]

    except requests.exceptions.RequestException:
        # Silencioso para não poluir, a busca no Google Imagens será a próxima etapa
        pass
    except Exception:
        pass

    # --- Tentativa 4: Se tudo acima falhar, busca no Google Imagens ---
    return search_google_images(product_name, service, cse_id)


def download_image(image_url, sku):
    """Baixa a imagem a partir de uma URL e salva com o SKU do produto."""
    if not image_url or "não encontrada" in image_url.lower() or "erro" in image_url.lower():
        return image_url
    try:
        response = requests.get(image_url, stream=True, timeout=15)
        response.raise_for_status()

        path = urlparse(image_url).path
        ext = os.path.splitext(path)[1] or '.jpg'
        filename = f"{sku}{ext}"

        os.makedirs(IMAGE_FOLDER, exist_ok=True)
        filepath = os.path.join(IMAGE_FOLDER, filename)

        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(8192):
                f.write(chunk)
        return filepath
    except Exception:
        return "Erro ao baixar imagem"

def generate_ai_descriptions(product_name):
    """Pede para a IA do Gemini criar uma descrição curta e uma longa (HTML)."""
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        prompt = f"""
        Para o produto '{product_name}', crie duas descrições de venda distintas:

        [DESCRIÇÃO CURTA]
        (Escreva aqui um resumo atraente e direto sobre o produto em 1 ou 2 linhas de texto puro)

        [DESCRIÇÃO LONGA HTML]
        (Escreva aqui uma descrição completa para e-commerce, formatada em HTML. Use parágrafos `<p>`, listas `<ul><li>...</li></ul>` e negrito `<strong>` para destacar características importantes. Não inclua as tags `<html>` ou `<body>`, apenas o conteúdo HTML interno que iria dentro de uma div de produto.)
        """
        response = model.generate_content(prompt)
        text = response.text.strip()

        short_desc_marker = "[DESCRIÇÃO CURTA]"
        long_desc_marker = "[DESCRIÇÃO LONGA HTML]"

        short_desc = "Erro ao extrair descrição curta"
        long_desc_html = "<p>Erro ao extrair descrição longa.</p>"

        if short_desc_marker in text and long_desc_marker in text:
            start_short = text.find(short_desc_marker) + len(short_desc_marker)
            end_short = text.find(long_desc_marker)
            short_desc = text[start_short:end_short].strip()

            start_long = text.find(long_desc_marker) + len(long_desc_marker)
            long_desc_html = text[start_long:].strip()

        return {"short": short_desc, "long_html": long_desc_html}
    except Exception as e:
        if 'quota' in str(e).lower():
             tqdm.write("  -> 🛑 ERRO DE QUOTA: O limite de requisições da API Gemini foi atingido.")
        else:
            tqdm.write(f"  -> Erro ao chamar a API do Gemini: {e}")
        return {"short": "Erro ao gerar descrição", "long_html": "<p>Erro ao gerar descrição.</p>"}

# --- ROTINA PRINCIPAL ---

def main():
    """Orquestra todo o processo, desde a leitura do arquivo até a gravação da saída."""
    if "COLE_SUA_CHAVE" in GOOGLE_API_KEY or "COLE_SEU_ID" in CSE_ID or "COLE_SUA_CHAVE" in GEMINI_API_KEY:
        print("🛑 ERRO: Parece que você não inseriu suas chaves de API no código.")
        return

    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"🛑 ERRO: Falha ao configurar a API Gemini. Verifique sua chave. Detalhe: {e}")
        return

    input_path = input("Qual o nome do arquivo de entrada? (ex: produtos.csv ou produtos.xlsx): ")
    if not os.path.exists(input_path):
        print(f"🛑 ERRO: Arquivo '{input_path}' não encontrado.")
        return

    try:
        if input_path.lower().endswith('.csv'):
            df = pd.read_csv(input_path, sep=';', on_bad_lines='skip', encoding='utf-8', engine='python')
        elif input_path.lower().endswith(('.xls', '.xlsx')):
            df = pd.read_excel(input_path)
        else:
            print(f"🛑 ERRO: Formato de arquivo não suportado. Use .csv ou .xlsx.")
            return
    except Exception as e:
        print(f"🛑 ERRO: Não consegui ler o arquivo '{input_path}'. Detalhes: {e}")
        return

    print(f"\n🚀 Começando! Encontrei {len(df)} produtos para processar.\n")
    processed_products = []
    service = build("customsearch", "v1", developerKey=GOOGLE_API_KEY)

    # Envolve o loop com tqdm para criar a barra de progresso
    for index, row in tqdm(df.iterrows(), total=len(df), desc="Processando produtos"):
        sku = row.get(COL_SKU, f'SKU_GEN_{index}')
        name = row.get(COL_NAME, 'PRODUTO_SEM_NOME')
        quantity = row.get(COL_QTY, 0)
        price = row.get(COL_PRICE, 0.0)

        # Atualiza a descrição da barra de progresso com o item atual
        tqdm.write(f"\n--- Processando: {name} ---")

        product_page_url = search_product_page_url(name, service, CSE_ID)
        
        image_url = extract_image_url(product_page_url, name, service, CSE_ID)
        
        if not image_url:
            image_url = "Imagem não encontrada"
            tqdm.write("  -> Nenhuma imagem encontrada mesmo após todas as tentativas.")

        local_image_path = download_image(image_url, sku)
        descriptions = generate_ai_descriptions(name)

        processed_products.append({
            "Nome": name,
            "Descrição Curta": descriptions["short"],
            "Descrição Longa (HTML)": descriptions["long_html"],
            "Preço": price,
            "Referência / SKU": sku,
            "Peso": 0,
            "Estoque": quantity,
            "URL da Imagem": image_url if "não encontrada" not in image_url else "https://placehold.co/600x400/eee/ccc?text=Imagem+Nao+Encontrada",
            "Situação": "Ativo",
            "Caminho Imagem Local": local_image_path,
            "URL Origem Página": product_page_url or "N/D",
        })
        
        time.sleep(1)

    if processed_products:
        output_df = pd.DataFrame(processed_products)
        output_df.to_excel(OUTPUT_FILENAME, index=False)
        print(f"\n🎉 Tudo pronto!")
        print(f"Seu novo arquivo Excel foi salvo como '{OUTPUT_FILENAME}'")
        print(f"As imagens foram baixadas para a pasta '{IMAGE_FOLDER}/'.")
    else:
        print("\n🤔 Nenhum produto foi processado.")


if __name__ == "__main__":
    main()