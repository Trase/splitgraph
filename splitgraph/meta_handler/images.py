from collections import defaultdict
from datetime import datetime

from psycopg2.extras import Json
from psycopg2.sql import SQL

from splitgraph.connection import get_connection
from splitgraph.exceptions import SplitGraphException
from splitgraph.meta_handler.common import select, insert
from splitgraph.meta_handler.objects import get_full_object_tree, get_object_for_table

IMAGE_COLS = "image_hash, parent_id, created, comment, provenance_type, provenance_data"


def get_all_images_parents(repository):
    with get_connection().cursor() as cur:
        cur.execute(select("images", IMAGE_COLS, "repository = %s AND namespace = %s") +
                    SQL(" ORDER BY created"), (repository.repository, repository.namespace))
        return cur.fetchall()


def get_closest_parent_image_object(repository, table, image):
    path = []
    object_id = get_object_for_table(repository, table, image, object_format='SNAP')
    if object_id is not None:
        return object_id, path

    object_id = get_object_for_table(repository, table, image, object_format='DIFF')

    # Here, we have to follow the object tree up until we encounter a parent of type SNAP -- firing a query
    # for every object is a massive bottleneck.
    # This could be done with a recursive PG query in the future, but currently we just load the whole tree
    # and crawl it in memory.
    object_tree = defaultdict(list)
    for oid, pid, object_format in get_full_object_tree():
        object_tree[oid].append((pid, object_format))

    while object_id is not None:
        path.append(object_id)
        for parent_id, object_format in object_tree[object_id]:
            # Check the _parent_'s format -- if it's a SNAP, we're done
            if object_tree[parent_id][0][1] == 'SNAP':
                return parent_id, path
            else:
                object_id = parent_id
                break  # Found 1 diff, will be added to the path at the next iteration.

    # We didn't find an actual snapshot for this table -- something's wrong with the object tree.
    raise SplitGraphException("Couldn't find a SNAP object for %s (malformed object tree)" % table)


def add_new_image(repository, parent_id, image, created=None, comment=None, provenance_type=None, provenance_data=None):
    with get_connection().cursor() as cur:
        cur.execute(insert("images", ("image_hash", "namespace", "repository", "parent_id", "created", "comment",
                                         "provenance_type", "provenance_data")),
                    (image, repository.namespace, repository.repository, parent_id, created or datetime.now(), comment,
                     provenance_type, Json(provenance_data)))


