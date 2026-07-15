import json

from django.shortcuts import render
from django.http import JsonResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import services


def dashboard_home(request):
    return render(request, "dashboard/index.html")


def api_summary(request):
    filters = services.get_filters(request)
    return JsonResponse(services.summary(filters))


def api_top_failures(request):
    filters = services.get_filters(request)
    try:
        limit = int(request.GET.get("limit", 10))
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 20))
    return JsonResponse(services.top_failures(filters, limit=limit), safe=False)


def api_channels(request):
    filters = services.get_filters(request)
    return JsonResponse(services.channel_summary(filters), safe=False)


def api_hourly_yield(request):
    filters = services.get_filters(request)
    return JsonResponse(services.hourly_yield(filters), safe=False)


def api_channel_hourly(request):
    filters = services.get_filters(request)
    return JsonResponse(services.channel_hourly(filters), safe=False)


def api_debug(request):
    return JsonResponse(services.debug_info())


def api_filters(request):
    return JsonResponse(services.distinct_filters())


def api_channel_matrix(request):
    filters = services.get_filters(request)
    return JsonResponse(services.failure_channel_matrix(filters))


def api_carrier_matrix(request):
    filters = services.get_filters(request)
    return JsonResponse(services.carrier_channel_matrix(filters))


def api_carrier_cycles(request):
    return JsonResponse(services.carrier_cycles(), safe=False)


def api_spc_distribution(request):
    filters = services.get_filters(request)
    step = request.GET.get("step", "").strip()
    if not step:
        return JsonResponse({"error": "step parameter required"}, status=400)

    usl = request.GET.get("usl") or None
    lsl = request.GET.get("lsl") or None

    if usl is not None:
        try:
            usl = float(usl)
        except ValueError:
            usl = None

    if lsl is not None:
        try:
            lsl = float(lsl)
        except ValueError:
            lsl = None

    return JsonResponse(services.parametric_distribution(filters, step, usl, lsl))


def api_spc_overview(request):
    filters = services.get_filters(request)
    return JsonResponse(services.step_cpk_overview(filters), safe=False)


@csrf_exempt
@require_POST
def api_spc_spec_set(request):
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("JSON inválido")

    step = str(body.get("step", "")).strip()
    if not step or len(step) > 64:
        return HttpResponseBadRequest("step obrigatório")

    def to_float_or_none(value):
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    usl = to_float_or_none(body.get("usl"))
    lsl = to_float_or_none(body.get("lsl"))
    unit = body.get("unit")
    unit = str(unit)[:16] if unit else None

    services.set_step_spec(step, usl=usl, lsl=lsl, unit=unit)
    return JsonResponse({"ok": True, "step": step, "usl": usl, "lsl": lsl, "unit": unit})


@csrf_exempt
@require_POST
def api_carrier_reset(request):
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("JSON inválido")

    carrier = str(body.get("carrier", "")).strip()
    if not carrier or len(carrier) > 64:
        return HttpResponseBadRequest("carrier obrigatório")

    notes = body.get("notes")
    if notes is not None:
        notes = str(notes)[:500]

    services.reset_carrier(carrier, notes)
    return JsonResponse({"ok": True, "carrier": carrier})


@csrf_exempt
@require_POST
def api_carrier_limit(request):
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("JSON inválido")

    carrier = str(body.get("carrier", "")).strip()
    if not carrier or len(carrier) > 64:
        return HttpResponseBadRequest("carrier obrigatório")

    limit = body.get("limit")
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return HttpResponseBadRequest("limit inválido")
        if limit < 1 or limit > 10000000:
            return HttpResponseBadRequest("limit fora do intervalo")

    services.set_carrier_limit(carrier, limit)
    return JsonResponse({"ok": True, "carrier": carrier, "limit": limit})
