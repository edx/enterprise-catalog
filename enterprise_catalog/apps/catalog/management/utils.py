""" Utility functions for catalog management """
from enterprise_catalog.apps.catalog.models import ContentMetadata


def iter_queryset_in_batches(queryset, batch_size):
    """
    Iterate an arbitrary queryset in PK-ordered batches.

    Unlike ``batch_by_pk(ModelClass, extra_filter=...)``, this helper accepts a
    fully-constructed queryset, so all existing predicates (including
    ``exclude(Exists(...))``, joins, distinct, etc.) are preserved.

    Yields:
        list: A list of model instances whose length is at most ``batch_size``.

    Example usage::

        for batch in iter_queryset_in_batches(ContentMetadata.objects.filter(...), batch_size=100):
            for obj in batch:
                ...
    """
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        raise ValueError('batch_size must be a positive integer')

    last_pk = None
    while True:
        batch_queryset = queryset.order_by('pk')
        if last_pk is not None:
            batch_queryset = batch_queryset.filter(pk__gt=last_pk)
        current_batch = list(batch_queryset[:batch_size])
        if not current_batch:
            break
        yield current_batch
        last_pk = current_batch[-1].pk


def get_all_content_keys():
    """
    Returns a list of content keys for all ContentMetadata objects.
    """
    all_content_metadata = ContentMetadata.objects.values_list('content_key', flat=True)
    return list(all_content_metadata)
