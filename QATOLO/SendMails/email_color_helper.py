"""
Helper para generar las variables de color de los templates de email.
Agrégalo a customers.py o a un módulo de utilidades.
"""
import json


def _hex_to_rgb(hex_color):
    """#113f67 → (17, 63, 103)"""
    h = hex_color.lstrip('#')
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def build_email_color_vars(business: dict) -> dict:
    """
    Genera las variables de color dinámicas para los templates HTML.
    
    Uso:
        color_vars = build_email_color_vars(business)
        context = {**color_vars, 'business_name': ..., 'transaction_id': ...}
        html = render_template('order_request.html', context)
    """
    palette = business.get('themePalette', {})
    if isinstance(palette, str):
        try:
            palette = json.loads(palette)
        except Exception:
            palette = {}

    cp = palette.get('primary',    '#113f67')
    cs = palette.get('secondary',  '#34699a')
    ca = palette.get('accent',     '#58a0c8')
    ch = palette.get('background', '#fdf5aa')

    pr, pg, pb = _hex_to_rgb(cp)
    ar, ag, ab = _hex_to_rgb(ca)
    hr, hg, hb = _hex_to_rgb(ch)

    def rgba(r, g, b, a):
        return f'rgba({r}, {g}, {b}, {a})'

    return {
        # Colores sólidos
        'color_primary':    cp,
        'color_secondary':  cs,
        'color_accent':     ca,
        'color_highlight':  ch,
        # Primary con transparencia
        'rgba_primary_10':  rgba(pr, pg, pb, 0.1),
        'rgba_primary_45':  rgba(pr, pg, pb, 0.45),
        # Accent con transparencia
        'rgba_accent_5':    rgba(ar, ag, ab, 0.05),
        'rgba_accent_10':   rgba(ar, ag, ab, 0.1),
        'rgba_accent_15':   rgba(ar, ag, ab, 0.15),
        'rgba_accent_20':   rgba(ar, ag, ab, 0.2),
        'rgba_accent_30':   rgba(ar, ag, ab, 0.3),
        'rgba_accent_40':   rgba(ar, ag, ab, 0.4),
        # Highlight con transparencia
        'rgba_highlight_5':  rgba(hr, hg, hb, 0.05),
        'rgba_highlight_10': rgba(hr, hg, hb, 0.1),
        'rgba_highlight_15': rgba(hr, hg, hb, 0.15),
        'rgba_highlight_20': rgba(hr, hg, hb, 0.2),
        'rgba_highlight_50': rgba(hr, hg, hb, 0.5),
        'rgba_highlight_90': rgba(hr, hg, hb, 0.9),
    }