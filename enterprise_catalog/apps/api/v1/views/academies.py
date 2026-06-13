import logging
from uuid import UUID

import crum
from django.conf import settings
from django.utils import translation
from django.utils.functional import cached_property
from edx_rest_framework_extensions.auth.jwt.authentication import (
    JwtAuthentication,
)
from rest_framework import permissions, status, viewsets
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import action
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework_xml.renderers import XMLRenderer

from enterprise_catalog.apps.academy.models import Academy
from enterprise_catalog.apps.api.v1.serializers import AcademySerializer
from enterprise_catalog.apps.catalog.constants import (
    PERMISSION_HAS_PROVISIONING_ADMIN_ACCESS,
)
from enterprise_catalog.apps.catalog.models import EnterpriseCatalog


logger = logging.getLogger(__name__)


class AcademiesReadOnlyViewSet(viewsets.ReadOnlyModelViewSet):
    """Viewset for Read Only operations on Academies"""

    authentication_classes = [JwtAuthentication, SessionAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    renderer_classes = [JSONRenderer, XMLRenderer]
    serializer_class = AcademySerializer
    lookup_field = "uuid"

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if "lang" in request.query_params:
            lang = request.query_params["lang"]
            if lang in settings.MODELTRANSLATION_LANGUAGES:
                translation.activate(lang)

    @cached_property
    def request_action(self):
        return getattr(self, "action", None)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        academy_uuid = str(self.kwargs["uuid"]) if "uuid" in self.kwargs else None
        enterprise_customer = self.request.GET.get("enterprise_customer", None)
        context.update(
            {"academy_uuid": academy_uuid, "enterprise_uuid": enterprise_customer}
        )
        return context

    def get_queryset(self):
        """
        Returns the queryset corresponding to all academies the requesting user has access to.
        """
        enterprise_customer = self.request.GET.get("enterprise_customer", False)
        all_academies = Academy.objects.all()
        if self.request_action == "list":
            if enterprise_customer:
                user_accessible_academy_uuids = []
                for academy in all_academies:
                    academy_associated_catalogs = academy.enterprise_catalogs.all()
                    enterprise_associated_catalogs = academy_associated_catalogs.filter(
                        enterprise_uuid=enterprise_customer
                    )
                    if enterprise_associated_catalogs:
                        user_accessible_academy_uuids.append(academy.uuid)
                return all_academies.filter(uuid__in=user_accessible_academy_uuids)
            else:
                return Academy.objects.none()

        return Academy.objects.all()

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[permissions.IsAuthenticated],
        url_path='associate-catalog',
    )
    def associate_catalog(self, request, uuid=None):
        """
        Associate an EnterpriseCatalog with an Academy.
        POST /api/v1/academies/{academy_uuid}/associate-catalog/
        Request JSON body:
            enterprise_catalog_uuid (str): UUID of the EnterpriseCatalog to associate.
        Returns:
            rest_framework.response.Response:
                200: On success. Idempotent — safe to call if the association already exists.
                400: If ``enterprise_catalog_uuid`` is missing from the request body.
                403: If the requesting user does not have provisioning admin access.
                404: If the academy or catalog UUID does not exist.
        """
        crum.set_current_request(request)
        if not request.user.has_perm(PERMISSION_HAS_PROVISIONING_ADMIN_ACCESS):
            self.permission_denied(request)
        academy = self.get_object()
        enterprise_catalog_uuid = request.data.get("enterprise_catalog_uuid")
        if not enterprise_catalog_uuid:
            return Response(
                {"detail": "Missing enterprise_catalog_uuid in request body."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Validate that enterprise_catalog_uuid is a valid UUID and corresponds to an existing EnterpriseCatalog.
        try:
            catalog_uuid = UUID(str(enterprise_catalog_uuid))
        except (ValueError, TypeError, AttributeError):
            return Response(
                {"detail": f"Invalid UUID: {enterprise_catalog_uuid}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if the catalog exists; if not, return 404.
        try:
            catalog = EnterpriseCatalog.objects.get(uuid=catalog_uuid)
        except EnterpriseCatalog.DoesNotExist:
            return Response(
                {"detail": f"EnterpriseCatalog with UUID {enterprise_catalog_uuid} does not exist."},
                status=status.HTTP_404_NOT_FOUND,
            )
        academy.enterprise_catalogs.add(catalog)
        logger.info(
            "Successfully associated EnterpriseCatalog %s with Academy %s.",
            enterprise_catalog_uuid,
            uuid,
        )
        return Response(
            {
                "detail": f"Successfully associated catalog {enterprise_catalog_uuid} with academy {uuid}."
            },
            status=status.HTTP_200_OK,
        )
