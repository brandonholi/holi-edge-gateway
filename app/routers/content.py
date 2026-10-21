import json
from datetime import datetime, timezone
from typing import Any, Optional

from typing import Literal

from fastapi import APIRouter, Path, Query
from pydantic import ValidationError
from redis.asyncio import Redis

from ..clients.redis_client import get_cache_redis
from ..core.deps import SedeRequerida
from ..core.errors import ApiError
from ..schemas.catalog import ProductosOut
from ..schemas.common import Paginacion
from ..schemas.content import (Banner, BannersOut, CategoriaChip, Mundo, MundoDetalle,
                               MundosOut, RecursoApp, RecursosAppOut, CategoriaApp,
                               CategoriasAppOut, SeccionHome, SeccionesHomeOut)
from .catalog import POR_PAGINA_MAX, con_stock_primero, hidratar_productos

router = APIRouter(prefix="/v1", tags=["Content"])


def ahora() -> datetime:
    """Current UTC time; tests replace it with monkeypatch."""
    return datetime.now(timezone.utc)


async def leer_json(redis: Redis, key: str) -> Optional[Any]:
    """JSON stored in a Redis string; None when missing or corrupt."""
    raw = await redis.get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _fecha(valor: str) -> datetime:
    fecha = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    if fecha.tzinfo is None:
        raise ValueError("naive date")
    return fecha


def visible_en_sede(item: dict, sede: str, momento: datetime) -> bool:
    """Store filter plus validity window (inclusive); empty `tiendas` means every store."""
    tiendas = item.get("tiendas") or []
    if tiendas and sede not in tiendas:
        return False
    inicio, fin = item.get("inicio"), item.get("fin")
    try:
        if inicio and momento < _fecha(inicio):
            return False
        if fin and momento > _fecha(fin):
            return False
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def _coleccion_valida(coleccion: Any) -> Optional[dict]:
    """The published collection, or None when missing or malformed."""
    categorias = coleccion.get("categorias", []) if isinstance(coleccion, dict) else None
    if not isinstance(categorias, list) or not all(
            isinstance(c, dict) and isinstance(c.get("id"), int) for c in categorias):
        return None
    return coleccion


async def _skus_de_coleccion(redis: Redis, sede: str, coleccion: dict,
                             categorias: list, con_sueltos: bool) -> tuple:
    """
    (catalog version, SKUs) of the collection in the sede: the categories with
    their descendants, plus the loose SKUs when asked, always intersected with
    the sede catalog and with stock first.
    """
    v_cat = await redis.get(f"v:cat:{sede}") or "1"
    base = f"cat:{v_cat}:{sede}"
    descendientes = sorted({d for c in categorias for d in c.get("descendientes", [c["id"]])})

    pipe = redis.pipeline()
    for d in descendientes:
        pipe.smembers(f"{base}:{d}")
    pipe.smembers(base)
    resultados = await pipe.execute()

    encontrados = set().union(*resultados[:len(descendientes)])
    if con_sueltos:
        encontrados |= set(coleccion.get("skus", []))
    encontrados &= resultados[-1]
    return v_cat, await con_stock_primero(redis, sede, sorted(encontrados))


@router.get("/collections/{coleccion_id}/products", response_model=ProductosOut)
async def productos_de_coleccion(
    sede: SedeRequerida,
    coleccion_id: int = Path(..., ge=1),
    categoria_id: Optional[int] = Query(None, ge=1),
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(50, ge=1, le=POR_PAGINA_MAX),
):
    """Products of a collection in the sede: its categories (with descendants) plus loose SKUs."""
    redis = get_cache_redis()
    coleccion = _coleccion_valida(await leer_json(redis, f"col:{coleccion_id}"))
    if not coleccion:
        raise ApiError("no-encontrado", f"La colección {coleccion_id} no existe.")

    categorias = coleccion.get("categorias", [])
    if categoria_id is not None:
        categorias = [c for c in categorias if c["id"] == categoria_id]
        if not categorias:
            raise ApiError("parametros-invalidos", errores=[
                {"campo": "categoria_id", "mensaje": "La categoría no pertenece a la colección."}])

    v_cat, ordenados = await _skus_de_coleccion(redis, sede, coleccion, categorias,
                                                con_sueltos=categoria_id is None)
    inicio = (pagina - 1) * por_pagina
    return ProductosOut(
        sede=sede,
        version_catalogo=int(v_cat),
        items=await hidratar_productos(redis, sede, ordenados[inicio:inicio + por_pagina]),
        paginacion=Paginacion(pagina=pagina, por_pagina=por_pagina, total=len(ordenados)),
    )


async def _mundos_visibles(redis: Redis, sede: str) -> list:
    """Worlds of `content:mundos` visible in the sede; malformed data is skipped."""
    crudo = await leer_json(redis, "content:mundos")
    if not isinstance(crudo, list):
        return []
    momento = ahora()
    visibles = []
    for item in crudo:
        if not isinstance(item, dict) or not visible_en_sede(item, sede, momento):
            continue
        try:
            visibles.append((item, Mundo(**item)))
        except (ValidationError, TypeError):
            continue
    if not visibles:
        return []
    # Defense against drift: a world whose collection is gone (archived) is a dead screen.
    pipe = redis.pipeline()
    for _, mundo in visibles:
        pipe.get(f"col:{mundo.coleccion_id}")
    existentes = await pipe.execute()
    return [v for v, raw in zip(visibles, existentes) if raw]


async def _arbol(redis: Redis, sede: str) -> dict:
    v_cat = await redis.get(f"v:cat:{sede}") or "1"
    return await redis.hgetall(f"tree:{v_cat}:{sede}")


@router.get("/banners", response_model=BannersOut)
async def listar_banners(
    sede: SedeRequerida,
    ubicacion: Literal["principal", "categoria"] = Query(...),
    categoria_id: Optional[int] = Query(None, ge=1),
):
    """Banners valid now in the sede; images whose destination is not in the sede are dropped."""
    if ubicacion == "categoria" and categoria_id is None:
        raise ApiError("parametros-invalidos", errores=[
            {"campo": "categoria_id", "mensaje": "Es obligatorio cuando ubicacion es categoria."}])

    return BannersOut(items=await _banners_validos(
        get_cache_redis(), sede,
        lambda b: b.ubicacion == ubicacion and (ubicacion != "categoria" or b.categoria_id == categoria_id)))


async def _banners_validos(redis: Redis, sede: str, aceptar) -> list:
    """
    Banners valid now in the sede that `aceptar` keeps, with the images whose
    destination does not exist in the sede dropped, and banners left empty out.
    """
    crudo = await leer_json(redis, "content:banners")
    if not isinstance(crudo, list):
        return []
    momento = ahora()

    candidatos = []
    for item in crudo:
        if not isinstance(item, dict) or not visible_en_sede(item, sede, momento):
            continue
        try:
            banner = Banner(**item)
        except (ValidationError, TypeError):
            continue
        if aceptar(banner):
            candidatos.append(banner)
    if not candidatos:
        return []

    # Each key is read once per request and shared by every destination check.
    v_cat = await redis.get(f"v:cat:{sede}") or "1"
    catalogo = await redis.smembers(f"cat:{v_cat}:{sede}")
    arbol = await redis.hgetall(f"tree:{v_cat}:{sede}")
    mundos = {m.id for _, m in await _mundos_visibles(redis, sede)}
    colecciones: dict = {}

    async def existe_coleccion(valor: str) -> bool:
        if valor not in colecciones:
            colecciones[valor] = bool(await redis.get(f"col:{valor}"))
        return colecciones[valor]

    async def destino_valido(tipo: str, valor: str) -> bool:
        if tipo == "producto":
            return valor in catalogo
        if tipo == "categoria":
            return valor in arbol
        if tipo == "mundo":
            return valor.isdigit() and int(valor) in mundos
        return await existe_coleccion(valor)

    salida = []
    for banner in candidatos:
        banner.imagenes = [i for i in banner.imagenes
                           if await destino_valido(i.destino.tipo, i.destino.id)]
        if banner.imagenes:
            salida.append(banner)
    return salida


@router.get("/worlds", response_model=MundosOut)
async def listar_mundos(sede: SedeRequerida):
    """Worlds valid now in the sede."""
    return MundosOut(items=[m for _, m in await _mundos_visibles(get_cache_redis(), sede)])


@router.get("/worlds/{mundo_id}", response_model=MundoDetalle)
async def detalle_mundo(sede: SedeRequerida, mundo_id: int = Path(..., ge=1)):
    """A world with the category chips of its collection that exist in the sede's tree."""
    redis = get_cache_redis()
    mundo = next((m for _, m in await _mundos_visibles(redis, sede) if m.id == mundo_id), None)
    if mundo is None:
        raise ApiError("no-encontrado", f"El mundo {mundo_id} no existe en la sede {sede}.")

    coleccion = await leer_json(redis, f"col:{mundo.coleccion_id}")
    crudas = coleccion.get("categorias", []) if isinstance(coleccion, dict) else []
    arbol = await _arbol(redis, sede)
    chips = [CategoriaChip(id=c["id"], nombre=c.get("nombre", ""))
             for c in crudas if isinstance(c, dict) and isinstance(c.get("id"), int)
             and str(c["id"]) in arbol]
    return MundoDetalle(**mundo.model_dump(), categorias=chips)


@router.get("/app-assets", response_model=RecursosAppOut)
async def listar_recursos_app():
    """
    Files the app shows outside the catalog: the cover of the first screen, the
    loading animation. Read at launch, before any sede, and only from Redis, so
    it answers even with Odoo down. The app downloads each file from the CDN and
    keeps it until its hash changes.
    """
    recursos = await leer_json(get_cache_redis(), "content:app_assets")
    items = []
    for item in recursos if isinstance(recursos, list) else []:
        try:
            items.append(RecursoApp(**item))
        except (ValidationError, TypeError):
            continue
    return RecursosAppOut(items=items)


DESTACADAS_MAX = 5


@router.get("/home-categories", response_model=CategoriasAppOut)
async def listar_categorias_app(
    sede: SedeRequerida,
    destacadas: bool = Query(False, description="true: solo las de la pantalla principal (hasta 5)"),
):
    """
    The categories chosen in the back office for the app's category screens, in
    their order. One with no products in the sede is left out, so a tap never
    opens an empty list. This is a showcase only: /catalog and the collections
    keep working with every category.
    """
    redis = get_cache_redis()
    configuradas = await leer_json(redis, "content:app_categories")
    arbol = await _arbol(redis, sede)
    items = []
    for item in configuradas if isinstance(configuradas, list) else []:
        try:
            categoria = CategoriaApp(**item)
        except (ValidationError, TypeError):
            continue
        if str(categoria.id) in arbol and (categoria.destacada or not destacadas):
            items.append(categoria)
    return CategoriasAppOut(items=items[:DESTACADAS_MAX] if destacadas else items)


PRODUCTOS_SECCION = (2, 8)


@router.get("/home-sections", response_model=SeccionesHomeOut)
async def listar_secciones_home(sede: SedeRequerida):
    """
    The blocks of the home screen, in order, ready to paint: header, banner and
    the first products of the collection in the sede. A section with no products
    in the sede is left out; one whose banner has nothing valid here keeps its
    products and goes without banner.
    """
    redis = get_cache_redis()
    crudo = await leer_json(redis, "content:home_sections")
    momento = ahora()
    visibles = [s for s in (crudo if isinstance(crudo, list) else [])
                if isinstance(s, dict) and visible_en_sede(s, sede, momento)]
    if not visibles:
        return SeccionesHomeOut(items=[])

    banners = {b.id: b for b in await _banners_validos(redis, sede, lambda b: b.ubicacion == "seccion")}
    minimo, maximo = PRODUCTOS_SECCION
    salida = []
    for seccion in visibles:
        try:
            coleccion_id = int(seccion["coleccion_id"])
            cantidad = min(max(int(seccion.get("cantidad") or minimo), minimo), maximo)
            titulo = str(seccion["titulo"])
        except (KeyError, TypeError, ValueError):
            continue
        coleccion = _coleccion_valida(await leer_json(redis, f"col:{coleccion_id}"))
        if not coleccion:
            continue
        _v, skus = await _skus_de_coleccion(redis, sede, coleccion, coleccion.get("categorias", []),
                                            con_sueltos=True)
        if not skus:
            continue
        salida.append(SeccionHome(
            id=seccion.get("id", 0), titulo=titulo,
            colores=[c for c in seccion.get("colores") or [] if isinstance(c, str)],
            banner=banners.get(seccion.get("banner_id")),
            productos=await hidratar_productos(redis, sede, skus[:cantidad]),
            coleccion_id=coleccion_id,
        ))
    return SeccionesHomeOut(items=salida)
