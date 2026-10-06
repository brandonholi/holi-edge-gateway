"""
Point-in-polygon and nearest-store resolution.

The square used throughout sits around Miraflores, Lima: latitude near -12,
longitude near -77, both negative. Those signs are the whole reason the model
validates bounds — a swapped pair lands in the Pacific and every customer would
be told we do not deliver.
"""
from app.core.geo import haversine_km, point_in_polygon, resolve_zone

# A square from (-12.13, -77.05) to (-12.10, -77.02)
CUADRADO = [
    (-12.13, -77.05),
    (-12.13, -77.02),
    (-12.10, -77.02),
    (-12.10, -77.05),
]


def _zona(sede, poligono, tienda_lat=0.0, tienda_lng=0.0, prioridad=10):
    lats = [p[0] for p in poligono]
    lngs = [p[1] for p in poligono]
    return {
        "zona_id": abs(hash(sede)) % 1000,
        "nombre": f"Zona {sede}",
        "prioridad": prioridad,
        "sede": sede,
        "tienda": f"Holi {sede}",
        "tienda_lat": tienda_lat,
        "tienda_lng": tienda_lng,
        "limites": {
            "lat_min": min(lats), "lat_max": max(lats),
            "lng_min": min(lngs), "lng_max": max(lngs),
        },
        "poligono": [{"lat": lat, "lng": lng} for lat, lng in poligono],
    }


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

def test_punto_interior_esta_dentro():
    assert point_in_polygon(-12.115, -77.035, CUADRADO) is True


def test_punto_exterior_esta_fuera():
    assert point_in_polygon(-12.20, -77.035, CUADRADO) is False
    assert point_in_polygon(-12.115, -77.30, CUADRADO) is False


def test_coordenadas_invertidas_quedan_fuera():
    """Swapping lat and lng must not accidentally match."""
    assert point_in_polygon(-77.035, -12.115, CUADRADO) is False


def test_poligono_degenerado_no_contiene_nada():
    assert point_in_polygon(-12.115, -77.035, [(-12.13, -77.05), (-12.10, -77.02)]) is False


def test_poligono_concavo_excluye_la_muesca():
    """A C-shape must not swallow the gap in its middle."""
    ce = [
        (-12.13, -77.05), (-12.13, -77.02), (-12.125, -77.02), (-12.125, -77.045),
        (-12.105, -77.045), (-12.105, -77.02), (-12.10, -77.02), (-12.10, -77.05),
    ]
    assert point_in_polygon(-12.128, -77.03, ce) is True   # brazo inferior
    assert point_in_polygon(-12.115, -77.03, ce) is False  # la muesca


def test_distancia_haversine_es_razonable():
    """Roughly a kilometre per hundredth of a degree of latitude."""
    d = haversine_km(-12.10, -77.03, -12.11, -77.03)
    assert 1.0 < d < 1.2
    assert haversine_km(-12.10, -77.03, -12.10, -77.03) == 0.0


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

def test_sin_zonas_no_hay_cobertura():
    """Before anyone draws a polygon, nowhere is covered. That is correct."""
    assert resolve_zone(-12.115, -77.035, []) is None


def test_una_zona_resuelve_su_sede():
    zonas = [_zona("LIM01", CUADRADO, tienda_lat=-12.115, tienda_lng=-77.03)]
    match = resolve_zone(-12.115, -77.035, zonas)
    assert match["zona"]["sede"] == "LIM01"
    assert match["distancia_km"] is not None


def test_zonas_superpuestas_eligen_la_tienda_mas_cercana():
    grande = [(-12.20, -77.10), (-12.20, -76.95), (-12.05, -76.95), (-12.05, -77.10)]
    zonas = [
        _zona("LIM02", grande, tienda_lat=-12.19, tienda_lng=-77.09),   # lejos
        _zona("LIM01", CUADRADO, tienda_lat=-12.115, tienda_lng=-77.035),  # encima
    ]
    match = resolve_zone(-12.115, -77.035, zonas)
    assert match["zona"]["sede"] == "LIM01"


def test_sin_coordenadas_de_tienda_manda_la_prioridad():
    """A store that cannot be measured falls back to the order set by hand."""
    grande = [(-12.20, -77.10), (-12.20, -76.95), (-12.05, -76.95), (-12.05, -77.10)]
    zonas = [
        _zona("LIM02", grande, prioridad=30),
        _zona("LIM01", CUADRADO, prioridad=5),
    ]
    match = resolve_zone(-12.115, -77.035, zonas)
    assert match["zona"]["sede"] == "LIM01"
    assert match["distancia_km"] is None


def test_los_limites_descartan_sin_recorrer_el_poligono():
    """The bounding box is a shortcut, so it must never change the answer."""
    zona = _zona("LIM01", CUADRADO, tienda_lat=-12.115, tienda_lng=-77.03)
    assert resolve_zone(-12.50, -77.035, [zona]) is None
    assert resolve_zone(-12.115, -77.035, [zona]) is not None


def test_vertice_malformado_no_rompe_la_resolucion():
    zona = _zona("LIM01", CUADRADO, tienda_lat=-12.115, tienda_lng=-77.03)
    zona["poligono"].append({"lat": -12.11})  # sin lng
    assert resolve_zone(-12.115, -77.035, [zona])["zona"]["sede"] == "LIM01"
