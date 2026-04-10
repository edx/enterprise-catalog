from django.conf import settings
from django.utils import translation
from django.utils.functional import cached_property
from edx_rest_framework_extensions.auth.jwt.authentication import (
    JwtAuthentication,
)
from rest_framework import permissions, viewsets
from rest_framework.authentication import SessionAuthentication
from rest_framework.renderers import JSONRenderer
from rest_framework_xml.renderers import XMLRenderer

from enterprise_catalog.apps.academy.models import Academy
from enterprise_catalog.apps.api.v1.serializers import AcademySerializer
from enterprise_catalog.apps.catalog.models import ContentMetadata


class AcademiesReadOnlyViewSet(viewsets.ReadOnlyModelViewSet):
    """ Viewset for Read Only operations on Academies """
    authentication_classes = [JwtAuthentication, SessionAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    renderer_classes = [JSONRenderer, XMLRenderer]
    serializer_class = AcademySerializer
    lookup_field = 'uuid'

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if 'lang' in request.query_params:
            lang = request.query_params['lang']
            if lang in settings.MODELTRANSLATION_LANGUAGES:
                translation.activate(lang)

    @cached_property
    def request_action(self):
        return getattr(self, 'action', None)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        academy_uuid = str(self.kwargs['uuid']) if 'uuid' in self.kwargs else None
        enterprise_customer = self.request.GET.get('enterprise_customer', None)
        context.update({'academy_uuid': academy_uuid, 'enterprise_uuid': enterprise_customer})
        return context

    def get_queryset(self):
        """
        Returns the queryset corresponding to all academies the requesting user has access to.
        """
        enterprise_customer = self.request.GET.get('enterprise_customer', False)
        all_academies = Academy.objects.all()
        if self.request_action == 'list':
            if enterprise_customer:
                user_accessible_academy_uuids = []
                for academy in all_academies:
                    academy_associated_catalogs = academy.enterprise_catalogs.all()
                    enterprise_associated_catalogs = academy_associated_catalogs.filter(
                        enterprise_uuid=enterprise_customer
                    )
                    if enterprise_associated_catalogs:
                        # Verify the academy has actual content in the enterprise catalog,
                        # not just a catalog association.
                        academy_content = ContentMetadata.objects.filter(tags__academies=academy)
                        catalog_query_ids = enterprise_associated_catalogs.values_list(
                            'catalog_query_id', flat=True,
                        )
                        if academy_content.filter(catalog_queries__in=catalog_query_ids).exists():
                            user_accessible_academy_uuids.append(academy.uuid)
                return all_academies.filter(uuid__in=user_accessible_academy_uuids)
            else:
                return Academy.objects.none()

        return Academy.objects.all()
