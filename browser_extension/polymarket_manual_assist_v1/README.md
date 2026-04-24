Carregue esta extensao no Chrome/Edge:

1. Abra `chrome://extensions` ou `edge://extensions`
2. Ative `Developer mode`
3. Clique em `Load unpacked`
4. Selecione esta pasta:
   `browser_extension/polymarket_manual_assist_v1`

Antes de abrir a Polymarket, rode:

```bash
python run_manual_signal_server_v1.py --qty 6
```

Esta extensao substitui o Tampermonkey e injeta o painel diretamente na pagina.
