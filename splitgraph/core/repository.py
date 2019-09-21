"""
Public API for managing images in a Splitgraph repository.
"""

import itertools
import logging
from contextlib import contextmanager
from datetime import datetime
from io import TextIOWrapper
from random import getrandbits
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union, Set, Sequence, cast

from psycopg2.sql import Composed
from psycopg2.sql import SQL, Identifier

from splitgraph.config import SPLITGRAPH_META_SCHEMA, SPLITGRAPH_API_SCHEMA, FDW_CLASS
from splitgraph.core import select
from splitgraph.core._common import insert
from splitgraph.core.fragment_manager import get_random_object_id, ExtraIndexInfo
from splitgraph.core.image import Image
from splitgraph.core.image_manager import ImageManager
from splitgraph.core.sql import validate_import_sql
from splitgraph.core.table import Table
from splitgraph.engine.postgres.engine import PostgresEngine
from splitgraph.exceptions import CheckoutError, EngineInitializationError, TableNotFoundError
from ._common import (
    manage_audit_triggers,
    set_head,
    manage_audit,
    aggregate_changes,
    slow_diff,
    prepare_publish_data,
    gather_sync_metadata,
    ResultShape,
)
from .engine import lookup_repository, get_engine
from .object_manager import ObjectManager
from .registry import publish_tag, PublishInfo


class Repository:
    """
    Splitgraph repository API
    """

    def __init__(
        self,
        namespace: str,
        repository: str,
        engine: Optional[PostgresEngine] = None,
        object_engine: Optional[PostgresEngine] = None,
        object_manager: None = None,
    ) -> None:
        self.namespace = namespace
        self.repository = repository

        self.engine = engine or get_engine()
        # Add an option to use a different engine class for storing cached table fragments (e.g. a different
        # PostgreSQL connection or even a different database engine altogether).
        self.object_engine = object_engine or self.engine
        self.images = ImageManager(self)

        # consider making this an injected/a singleton for a given engine
        # since it's global for the whole engine but calls (e.g. repo.objects.cleanup()) make it
        # look like it's the manager for objects related to a repo.
        self.objects = object_manager or ObjectManager(
            object_engine=self.object_engine, metadata_engine=self.engine
        )

    @classmethod
    def from_template(
        cls,
        template: "Repository",
        namespace: Optional[str] = None,
        repository: None = None,
        engine: Optional[PostgresEngine] = None,
        object_engine: Optional[PostgresEngine] = None,
    ) -> "Repository":
        """Create a Repository from an existing one replacing some of its attributes."""
        # If engine has been overridden but not object_engine, also override the object_engine (maintain
        # the intended behaviour of overriding engine repointing the whole repository)
        return cls(
            namespace or template.namespace,
            repository or template.repository,
            engine or template.engine,
            object_engine or engine or template.object_engine,
        )

    @classmethod
    def from_schema(cls, schema: str) -> "Repository":
        """Convert a Postgres schema name of the format `namespace/repository` to a Splitgraph repository object."""
        if "/" in schema:
            namespace, repository = schema.split("/")
            return cls(namespace, repository)
        return cls("", schema)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Repository):
            return NotImplemented
        return self.namespace == other.namespace and self.repository == other.repository

    def to_schema(self) -> str:
        """Returns the engine schema that this repository gets checked out into."""
        return self.namespace + "/" + self.repository if self.namespace else self.repository

    def __repr__(self) -> str:
        repr = "Repository %s on %s" % (self.to_schema(), self.engine.name)
        if self.engine != self.object_engine:
            repr += " (object engine %s)" % self.object_engine.name
        return repr

    __str__ = to_schema

    def __hash__(self) -> int:
        return hash(self.namespace) * hash(self.repository)

    # --- GENERAL REPOSITORY MANAGEMENT ---

    def rollback_engines(self) -> None:
        """
        Rollback the underlying transactions on both engines that the repository uses.
        """
        self.engine.rollback()
        if self.engine != self.object_engine:
            self.object_engine.rollback()

    def commit_engines(self) -> None:
        """
        Commit the underlying transactions on both engines that the repository uses.
        """
        self.engine.commit()
        if self.engine != self.object_engine:
            self.object_engine.commit()

    @manage_audit
    def init(self) -> None:
        """
        Initializes an empty repo with an initial commit (hash 0000...)
        """
        self.object_engine.create_schema(self.to_schema())
        initial_image = "0" * 64
        self.engine.run_sql(
            insert("images", ("image_hash", "namespace", "repository", "parent_id", "created")),
            (initial_image, self.namespace, self.repository, None, datetime.now()),
        )
        # Strictly speaking this is redundant since the checkout (of the "HEAD" commit) updates the tag table.
        self.engine.run_sql(
            insert("tags", ("namespace", "repository", "image_hash", "tag")),
            (self.namespace, self.repository, initial_image, "HEAD"),
        )

    def delete(self, unregister: bool = True, uncheckout: bool = True) -> None:
        """
        Discards all changes to a given repository and optionally all of its history,
        as well as deleting the Postgres schema that it might be checked out into.
        Doesn't delete any cached physical objects.

        After performing this operation, this object becomes invalid and must be discarded,
        unless init() is called again.

        :param unregister: Whether to purge repository history/metadata
        :param uncheckout: Whether to delete the actual checked out repo
        """
        # Make sure to discard changes to this repository if they exist, otherwise they might
        # be applied/recorded if a new repository with the same name appears.
        if uncheckout:
            # If we're talking to a bare repo / a remote that doesn't have checked out repositories,
            # there's no point in touching the audit trigger.
            try:
                self.object_engine.discard_pending_changes(self.to_schema())
            except EngineInitializationError:
                # If the audit trigger doesn't exist,
                logging.warning(
                    "Audit triggers don't exist on engine %s, not running uncheckout.",
                    self.object_engine,
                )
            else:
                # Dispose of the foreign servers (LQ FDW, other FDWs) for this schema if it exists
                # (otherwise its connection won't be recycled and we can get deadlocked).
                self.object_engine.run_sql(
                    SQL("DROP SERVER IF EXISTS {} CASCADE").format(
                        Identifier("%s_lq_checkout_server" % self.to_schema())
                    )
                )
                self.object_engine.run_sql(
                    SQL("DROP SERVER IF EXISTS {} CASCADE").format(
                        Identifier(self.to_schema() + "_server")
                    )
                )
                self.object_engine.run_sql(
                    SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(Identifier(self.to_schema()))
                )
        if unregister:
            # Use the API call in case we're deleting a remote repository.
            self.engine.run_sql(
                SQL("SELECT {}.delete_repository(%s,%s)").format(Identifier(SPLITGRAPH_API_SCHEMA)),
                (self.namespace, self.repository),
            )

            # On local repos, also forget about the repository's upstream.
            if self.engine.table_exists(SPLITGRAPH_META_SCHEMA, "upstream"):
                self.engine.run_sql(
                    SQL("DELETE FROM {}.{} WHERE namespace = %s AND repository = %s").format(
                        Identifier(SPLITGRAPH_META_SCHEMA), Identifier("upstream")
                    ),
                    (self.namespace, self.repository),
                )
        self.engine.commit()

    @property
    def upstream(self):
        """
        The remote upstream repository that this local repository tracks.
        """
        result = self.engine.run_sql(
            select(
                "upstream",
                "remote_name, remote_namespace, remote_repository",
                "namespace = %s AND repository = %s",
            ),
            (self.namespace, self.repository),
            return_shape=ResultShape.ONE_MANY,
        )
        if result is None:
            return result
        return Repository(namespace=result[1], repository=result[2], engine=get_engine(result[0]))

    @upstream.setter
    def upstream(self, remote_repository: "Repository"):
        """
        Sets the upstream remote + repository that this repository tracks.

        :param remote_repository: Remote Repository object
        """
        self.engine.run_sql(
            SQL(
                "INSERT INTO {0}.upstream (namespace, repository, "
                "remote_name, remote_namespace, remote_repository) VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (namespace, repository) DO UPDATE SET "
                "remote_name = excluded.remote_name, remote_namespace = excluded.remote_namespace, "
                "remote_repository = excluded.remote_repository WHERE "
                "upstream.namespace = excluded.namespace "
                "AND upstream.repository = excluded.repository"
            ).format(Identifier(SPLITGRAPH_META_SCHEMA)),
            (
                self.namespace,
                self.repository,
                remote_repository.engine.name,
                remote_repository.namespace,
                remote_repository.repository,
            ),
        )

    @upstream.deleter
    def upstream(self):
        """
        Deletes the upstream remote + repository for a local repository.
        """
        self.engine.run_sql(
            SQL("DELETE FROM {0}.upstream WHERE namespace = %s AND repository = %s").format(
                Identifier(SPLITGRAPH_META_SCHEMA)
            ),
            (self.namespace, self.repository),
            return_shape=None,
        )

    # --- COMMITS / CHECKOUTS ---

    @contextmanager
    def materialized_table(
        self, table_name: str, image_hash: Optional[str]
    ) -> Iterator[Tuple[str, str]]:
        """A context manager that returns a pointer to a read-only materialized table in a given image.
        The table is deleted on exit from the context manager.

        :param table_name: Name of the table
        :param image_hash: Image hash to materialize
        :return: (schema, table_name) where the materialized table is located.
        """
        if image_hash is None:
            # No image hash -- just return the current staging table.
            yield self.to_schema(), table_name
            return  # make sure we don't fall through after the user is finished

        table = self.images.by_hash(image_hash).get_table(table_name)
        # Materialize the table even if it's a single object to discard the upsert-delete flag.
        new_id = get_random_object_id()
        table.materialize(new_id, destination_schema=SPLITGRAPH_META_SCHEMA)
        try:
            yield SPLITGRAPH_META_SCHEMA, new_id
        finally:
            # Maybe some cache management/expiry strategies here
            self.object_engine.delete_table(SPLITGRAPH_META_SCHEMA, new_id)
            self.object_engine.commit()

    @property
    def head_strict(self) -> Image:
        """Return the HEAD image for the repository. Raise an exception if the repository
         isn't checked out."""
        return cast(Image, self.images.by_tag("HEAD", raise_on_none=True))

    @property
    def head(self) -> Optional[Image]:
        """Return the HEAD image for the repository or None if the repository isn't checked out."""
        return self.images.by_tag("HEAD", raise_on_none=False)

    @manage_audit
    def uncheckout(self, force: bool = False) -> None:
        """
        Deletes the schema that the repository is checked out into

        :param force: Discards all pending changes to the schema.
        """
        if not self.head:
            return
        if self.has_pending_changes():
            if not force:
                raise CheckoutError(
                    "{0} has pending changes! Pass force=True or do sgr checkout -f {0}:HEAD".format(
                        self.to_schema()
                    )
                )
            logging.warning("%s has pending changes, discarding...", self.to_schema())

        # Delete the schema and remove the HEAD tag
        self.delete(unregister=False, uncheckout=True)
        self.head.delete_tag("HEAD")

    def commit(
        self,
        image_hash: Optional[str] = None,
        comment: Optional[str] = None,
        snap_only: bool = False,
        chunk_size: Optional[int] = 10000,
        split_changeset: bool = False,
        extra_indexes: Optional[ExtraIndexInfo] = None,
    ) -> Image:
        """
        Commits all pending changes to a given repository, creating a new image.

        :param image_hash: Hash of the commit. Chosen by random if unspecified.
        :param comment: Optional comment to add to the commit.
        :param snap_only: If True, will store the table as a full snapshot instead of delta compression
        :param chunk_size: For tables that are stored as snapshots (new tables and where `snap_only` has been passed,
            the table will be split into fragments of this many rows.
        :param split_changeset: If True, splits the changeset into multiple fragments based on
            the PK regions spanned by the current table fragments. For example, if the original table
            consists of 2 fragments, first spanning rows 1-10000, second spanning rows 10001-20000 and the
            change alters rows 1, 10001 and inserts a row with PK 20001, this will record the change as
            3 fragments: one inheriting from the first original fragment, one inheriting from the second
            and a brand new fragment. This increases the number of fragments in total but means that fewer rows
            will need to be scanned to satisfy a query.
            If False, the changeset will be stored as a single fragment inheriting from the last fragment in the
            table.
        :param extra_indexes: Dictionary of {table: index_type: column: index_specific_kwargs}.
        :return: The newly created Image object.
        """

        logging.info("Committing %s...", self.to_schema())

        self.object_engine.commit()
        manage_audit_triggers(self.engine, self.object_engine)

        # HEAD can be None (if this is the first commit in this repository)
        head = self.head
        if image_hash is None:
            # Generate a random hexadecimal hash for new images
            image_hash = "{:064x}".format(getrandbits(256))

        self.images.add(head.image_hash if head else None, image_hash, comment=comment)
        self._commit(
            head,
            image_hash,
            snap_only=snap_only,
            chunk_size=chunk_size,
            split_changeset=split_changeset,
            extra_indexes=extra_indexes,
        )

        set_head(self, image_hash)
        manage_audit_triggers(self.engine, self.object_engine)
        self.object_engine.commit()
        self.engine.commit()
        return self.images.by_hash(image_hash)

    def _commit(
        self,
        head: Optional[Image],
        image_hash: str,
        snap_only: bool = False,
        chunk_size: Optional[int] = 10000,
        split_changeset: bool = False,
        schema: None = None,
        extra_indexes: Optional[ExtraIndexInfo] = None,
    ) -> None:
        """
        Reads the recorded pending changes to all tables in a given checked-out image,
        conflates them and possibly stores them as new object(s) as follows:

            * If a table has been created or there has been a schema change, it's only stored as a full snapshot.
            * If a table hasn't changed since the last revision, no new objects are created and it's linked to the
                previous objects belonging to the last revision.
            * Otherwise, the table is stored as a conflated (1 change per PK) patch.

        :param head: Current HEAD image to base the commit on.
        :param image_hash: Hash of the image to commit changes under.
        :param snap_only: If True, only stores the table as a snapshot.
        :param chunk_size: Split table snapshots into chunks of this size (None to disable)
        :param split_changeset: Split deltas to match original table snapshot boundaries
        :param schema: Schema that the image is checked out into. By default, `namespace/repository` is used.
        :param extra_indexes: Dictionary of {table: index_type: column: index_specific_kwargs}.
        """
        schema = schema or self.to_schema()
        extra_indexes: ExtraIndexInfo = extra_indexes or {}

        changed_tables = self.object_engine.get_changed_tables(schema)
        for table in self.object_engine.get_all_tables(schema):
            try:
                table_info: Optional[Table] = head.get_table(table) if head else None
            except TableNotFoundError:
                table_info = None

            # Store as a full copy if this is a new table, there's been a schema change or we were told to.
            # This is obviously wasteful (say if just one column has been added/dropped or we added a PK,
            # but it's a starting point to support schema changes.
            if (
                not table_info
                or snap_only
                or table_info.table_schema
                != self.object_engine.get_full_table_schema(schema, table)
            ):
                self.objects.record_table_as_base(
                    self,
                    table,
                    image_hash,
                    chunk_size=chunk_size,
                    source_schema=schema,
                    extra_indexes=extra_indexes.get(table),
                )
                continue

            # If the table has changed, look at the audit log and store it as a delta.
            if table in changed_tables:
                self.objects.record_table_as_patch(
                    table_info,
                    schema,
                    image_hash,
                    split_changeset=split_changeset,
                    extra_indexes=extra_indexes.get(table),
                )
                continue

            # If the table wasn't changed, point the image to the old table
            self.objects.register_tables(
                self, [(image_hash, table, table_info.table_schema, table_info.objects)]
            )

        # Make sure that all pending changes have been discarded by this point (e.g. if we created just a snapshot for
        # some tables and didn't consume the audit log).
        # NB if we allow partial commits, this will have to be changed (only discard for committed tables).
        self.object_engine.discard_pending_changes(schema)

    def has_pending_changes(self) -> bool:
        """
        Detects if the repository has any pending changes (schema changes, table additions/deletions, content changes).
        """
        head = self.head
        if not head:
            # If the repo isn't checked out, no point checking for changes.
            return False
        for table in self.object_engine.get_all_tables(self.to_schema()):
            if self.diff(table, head.image_hash, None, aggregate=True) != (0, 0, 0):
                return True
        return False

    # --- TAG AND IMAGE MANAGEMENT ---

    def get_all_hashes_tags(self) -> List[Tuple[Optional[str], str]]:
        """
        Gets all tagged images and their hashes in a given repository.

        :return: List of (image_hash, tag)
        """
        return cast(
            List[Tuple[Optional[str], str]],
            self.engine.run_sql(
                select(
                    "get_tagged_images",
                    "image_hash, tag",
                    schema=SPLITGRAPH_API_SCHEMA,
                    table_args="(%s,%s)",
                ),
                (self.namespace, self.repository),
            ),
        )

    def set_tags(self, tags: Dict[str, Optional[str]]) -> None:
        """
        Sets tags for multiple images.

        :param tags: List of (image_hash, tag)
        """
        for tag, image_id in tags.items():
            if tag != "HEAD":
                assert image_id is not None
                self.images.by_hash(image_id).tag(tag)

    def run_sql(
        self,
        sql: Union[Composed, str],
        arguments: Optional[Any] = None,
        return_shape: ResultShape = ResultShape.MANY_MANY,
    ) -> Any:
        """Execute an arbitrary SQL statement inside of this repository's checked out schema."""
        self.object_engine.run_sql("SET search_path TO %s", (self.to_schema(),))
        result = self.object_engine.run_sql(sql, arguments=arguments, return_shape=return_shape)
        self.object_engine.run_sql("SET search_path TO public")
        return result

    def dump(self, stream: TextIOWrapper, exclude_object_contents: bool = False) -> None:
        """
        Creates an SQL dump with the metadata required for the repository and all of its objects.

        :param stream: Stream to dump the data into.
        :param exclude_object_contents: Only dump the metadata but not the actual object contents.
        """
        # First, go through the metadata tables required to reconstruct the repository.
        stream.write("--\n-- Images --\n--\n")
        self.engine.dump_table_sql(
            SPLITGRAPH_META_SCHEMA,
            "images",
            stream,
            where="namespace = %s AND repository = %s",
            where_args=(self.namespace, self.repository),
        )

        # Add objects (need to come before tables: we check that objects for inserted tables are registered.
        required_objects: Set[str] = set()
        for image in self.images:
            for table_name in image.get_tables():
                required_objects.update(image.get_table(table_name).objects)

        object_qual = (
            "object_id IN (" + ",".join(itertools.repeat("%s", len(required_objects))) + ")"
        )

        stream.write("\n--\n-- Objects --\n--\n")
        # To avoid conflicts, we just delete the object records if they already exist
        with self.engine.connection.cursor() as cur:
            for table_name in ("objects", "object_locations"):
                stream.write(
                    cur.mogrify(
                        SQL("DELETE FROM {}.{} WHERE ").format(
                            Identifier(SPLITGRAPH_META_SCHEMA), Identifier(table_name)
                        )
                        + SQL(object_qual),
                        list(required_objects),
                    ).decode("utf-8")
                )
                stream.write(";\n\n")
                self.engine.dump_table_sql(
                    SPLITGRAPH_META_SCHEMA,
                    table_name,
                    stream,
                    where=object_qual,
                    where_args=list(required_objects),
                )

        stream.write("\n--\n-- Tables --\n--\n")
        self.engine.dump_table_sql(
            SPLITGRAPH_META_SCHEMA,
            "tables",
            stream,
            where="namespace = %s AND repository = %s",
            where_args=(self.namespace, self.repository),
        )

        stream.write("\n--\n-- Tags --\n--\n")
        self.engine.dump_table_sql(
            SPLITGRAPH_META_SCHEMA,
            "tags",
            stream,
            where="namespace = %s AND repository = %s AND tag != 'HEAD'",
            where_args=(self.namespace, self.repository),
        )

        if not exclude_object_contents:
            with self.engine.connection.cursor() as cur:
                stream.write("\n--\n-- Object contents --\n--\n")

                # Finally, dump the actual objects
                for object_id in required_objects:
                    stream.write(
                        cur.mogrify(
                            SQL("DROP FOREIGN TABLE IF EXISTS {}.{};\n").format(
                                Identifier(SPLITGRAPH_META_SCHEMA), Identifier(object_id)
                            )
                        ).decode("utf-8")
                    )
                    self.object_engine.dump_object(object_id, stream, schema=SPLITGRAPH_META_SCHEMA)
                    stream.write("\n")

    # --- IMPORTING TABLES ---

    @manage_audit
    def import_tables(
        self,
        tables: Sequence[str],
        source_repository: "Repository",
        source_tables: Sequence[str],
        image_hash: Optional[str] = None,
        foreign_tables: bool = False,
        do_checkout: bool = True,
        target_hash: Optional[str] = None,
        table_queries: Optional[Sequence[bool]] = None,
        parent_hash: Optional[str] = None,
        wrapper: Optional[str] = FDW_CLASS,
    ) -> str:
        """
        Creates a new commit in target_repository with one or more tables linked to already-existing tables.
        After this operation, the HEAD of the target repository moves to the new commit and the new tables are
        materialized.

        :param tables: If not empty, must be the list of the same length as `source_tables` specifying names to store
            them under in the target repository.
        :param source_repository: Repository to import tables from.
        :param source_tables: List of tables to import. If empty, imports all tables.
        :param image_hash: Image hash in the source repository to import tables from.
            Uses the current source HEAD by default.
        :param foreign_tables: If True, copies all source tables to create a series of new snapshots instead of
            treating them as Splitgraph-versioned tables. This is useful for adding brand new tables
            (for example, from an FDW-mounted table).
        :param do_checkout: If False, doesn't check out the newly created image.
        :param target_hash: Hash of the new image that tables is recorded under. If None, gets chosen at random.
        :param table_queries: If not [], it's treated as a Boolean mask showing which entries in the `tables` list are
            instead SELECT SQL queries that form the target table. The queries have to be non-schema qualified and work
            only against tables in the source repository. Each target table created is the result of the respective SQL
            query. This is committed as a new snapshot.
        :param parent_hash: If not None, must be the hash of the image to base the new image on.
            Existing tables from the parent image are preserved in the new image. If None, the current repository
            HEAD is used.
        :param wrapper: Override the default class for the layered querying foreign data wrapper.
        :return: Hash that the new image was stored under.
        """
        # Sanitize/validate the parameters and call the internal function.
        if table_queries is None:
            table_queries = []
        target_hash = target_hash or "{:064x}".format(getrandbits(256))

        image: Optional[Image]
        if not foreign_tables:
            image = (
                source_repository.images.by_hash(image_hash)
                if image_hash
                else source_repository.head_strict
            )
        else:
            image = None

        if not source_tables:
            assert image is not None
            source_tables = (
                image.get_tables()
                if not foreign_tables
                else source_repository.object_engine.get_all_tables(source_repository.to_schema())
            )
        if not tables:
            if table_queries:
                raise ValueError("target_tables has to be defined if table_queries is True!")
            tables = source_tables
        if not table_queries:
            table_queries = [False] * len(tables)
        if len(tables) != len(source_tables) or len(source_tables) != len(table_queries):
            raise ValueError("tables, source_tables and table_queries have mismatching lengths!")

        if parent_hash:
            existing_tables = self.images[parent_hash].get_tables()
        else:
            parent_hash = self.head.image_hash if self.head else None
            existing_tables = self.object_engine.get_all_tables(self.to_schema())
        clashing = [t for t in tables if t in existing_tables]
        if clashing:
            raise ValueError("Table(s) %r already exist(s) at %s!" % (clashing, self))

        return self._import_tables(
            image,
            tables,
            source_repository,
            target_hash,
            source_tables,
            do_checkout,
            table_queries,
            foreign_tables,
            parent_hash,
            wrapper,
        )

    def _import_tables(
        self,
        image: Optional[Image],
        tables: Sequence[str],
        source_repository: "Repository",
        target_hash: str,
        source_tables: Sequence[str],
        do_checkout: bool,
        table_queries: Sequence[bool],
        foreign_tables: bool,
        base_hash: Optional[str],
        wrapper: Optional[str],
    ) -> str:
        # This importing route only supported between local repos.
        assert self.engine == source_repository.engine
        assert self.object_engine == source_repository.object_engine
        if do_checkout:
            self.object_engine.create_schema(self.to_schema())

        self.images.add(
            base_hash, target_hash, comment="Importing %s from %s" % (tables, source_repository)
        )

        # Materialize the actual tables in the target repository and register them.
        for source_table, target_table, is_query in zip(source_tables, tables, table_queries):
            # For foreign tables/SELECT queries, we define a new object/table instead.
            if is_query and not foreign_tables:
                # If we're importing a query from another Splitgraph image, we can use LQ to satisfy it.
                # This could get executed for the whole import batch as opposed to for every import query
                # but the overhead of setting up an LQ schema is fairly small.
                assert image is not None
                with image.query_schema(wrapper=wrapper) as tmp_schema:
                    self._import_new_table(
                        tmp_schema, source_table, target_hash, target_table, is_query, do_checkout
                    )
            elif foreign_tables:
                self._import_new_table(
                    source_repository.to_schema(),
                    source_table,
                    target_hash,
                    target_table,
                    is_query,
                    do_checkout,
                )
            else:
                assert image is not None
                table_obj = image.get_table(source_table)
                self.objects.register_tables(
                    self, [(target_hash, target_table, table_obj.table_schema, table_obj.objects)]
                )
                if do_checkout:
                    table_obj.materialize(target_table, destination_schema=self.to_schema())
        # Register the existing tables at the new commit as well.
        if base_hash is not None:
            # Maybe push this into the tables API (currently have to make 2 queries)
            self.engine.run_sql(
                SQL(
                    """INSERT INTO {0}.tables (namespace, repository, image_hash,
                    table_name, table_schema, object_ids) (SELECT %s, %s, %s, table_name, table_schema, object_ids
                    FROM {0}.tables WHERE namespace = %s AND repository = %s AND image_hash = %s)"""
                ).format(Identifier(SPLITGRAPH_META_SCHEMA)),
                (
                    self.namespace,
                    self.repository,
                    target_hash,
                    self.namespace,
                    self.repository,
                    base_hash,
                ),
            )
        if do_checkout:
            set_head(self, target_hash)
        return target_hash

    def _import_new_table(
        self,
        source_schema: str,
        source_table: str,
        target_hash: str,
        target_table: str,
        is_query: bool,
        do_checkout: bool,
    ) -> List[str]:
        # First, import the query (or the foreign table) into a temporary table.
        tmp_object_id = get_random_object_id()
        if is_query:
            # is_query precedes foreign_tables: if we're importing using a query, we don't care if it's a
            # foreign table or not since we're storing it as a full snapshot.
            validate_import_sql(source_table)
            self.object_engine.run_sql_in(
                source_schema,
                SQL("CREATE TABLE {}.{} AS ").format(
                    Identifier(SPLITGRAPH_META_SCHEMA), Identifier(tmp_object_id)
                )
                + SQL(source_table),
            )
        else:
            self.object_engine.copy_table(
                source_schema, source_table, SPLITGRAPH_META_SCHEMA, tmp_object_id
            )

        # This is kind of a waste: if the table is indeed new (and fits in one chunk), the fragment manager will copy it
        # over once again and give it the new object ID. Maybe the fragment manager could rename the table in this case.
        actual_objects = self.objects.record_table_as_base(
            self,
            target_table,
            target_hash,
            source_schema=SPLITGRAPH_META_SCHEMA,
            source_table=tmp_object_id,
        )
        self.object_engine.delete_table(SPLITGRAPH_META_SCHEMA, tmp_object_id)
        if do_checkout:
            self.images.by_hash(target_hash).get_table(target_table).materialize(
                target_table, self.to_schema()
            )
        return actual_objects

    # --- SYNCING WITH OTHER REPOSITORIES ---

    def push(
        self,
        remote_repository: Optional["Repository"] = None,
        handler: str = "DB",
        handler_options: Optional[Dict[str, Any]] = None,
    ) -> "Repository":
        """
        Inverse of ``pull``: Pushes all local changes to the remote and uploads new objects.

        :param remote_repository: Remote repository to push changes to. If not specified, the current
            upstream is used.
        :param handler: Name of the handler to use to upload objects. Use `DB` to push them to the remote or `S3`
            to store them in an S3 bucket.
        :param handler_options: Extra options to pass to the handler. For example, see
            :class:`splitgraph.hooks.s3.S3ExternalObjectHandler`.
        """
        remote_repository = remote_repository or self.upstream
        if not remote_repository:
            raise ValueError(
                "No remote repository specified and no upstream found for %s!" % self.to_schema()
            )

        try:
            _sync(
                target=remote_repository,
                source=self,
                download=False,
                handler=handler,
                handler_options=handler_options,
            )

            if not self.upstream:
                self.upstream = remote_repository
                logging.info("Setting upstream for %s to %s.", self, remote_repository)
        finally:
            # Don't commit the connection here: _sync is supposed to do it itself
            # after a successful push/pull.
            remote_repository.engine.close()
        return remote_repository

    def pull(self, download_all: Optional[bool] = False) -> None:
        """
        Synchronizes the state of the local Splitgraph repository with its upstream, optionally downloading all new
        objects created on the remote.

        :param download_all: If True, downloads all objects and stores them locally. Otherwise, will only download
            required objects when a table is checked out.
        """
        if not self.upstream:
            raise ValueError("No upstream found for repository %s!" % self.to_schema())

        clone(remote_repository=self.upstream, local_repository=self, download_all=download_all)

    def publish(
        self,
        tag: str,
        remote_repository: Optional["Repository"] = None,
        readme: str = "",
        include_provenance: bool = True,
        include_table_previews: bool = True,
    ) -> None:
        """
        Summarizes the data on a previously-pushed repository and makes it available in the catalog.

        :param tag: Image tag. Only images with tags can be published.
        :param remote_repository: Remote Repository object (uses the upstream if unspecified)
        :param readme: Optional README for the repository.
        :param include_provenance: If False, doesn't include the dependencies of the image
        :param include_table_previews: Whether to include data previews for every table in the image.
        """
        remote_repository = remote_repository or self.upstream
        if not remote_repository:
            raise ValueError(
                "No remote repository specified and no upstream found for %s!" % self.to_schema()
            )

        image = self.images[tag]
        logging.info("Publishing %s:%s (%s)", self, image.image_hash, tag)

        dependencies = (
            [((r.namespace, r.repository), i) for r, i in image.provenance()]
            if include_provenance
            else None
        )
        previews, schemata = prepare_publish_data(image, self, include_table_previews)

        try:
            publish_tag(
                remote_repository,
                tag,
                PublishInfo(
                    image_hash=image.image_hash,
                    published=datetime.now(),
                    provenance=dependencies,
                    readme=readme,
                    schemata=schemata,
                    previews=previews if include_table_previews else None,
                ),
            )
            remote_repository.engine.commit()
        finally:
            remote_repository.engine.close()

    def diff(
        self,
        table_name: str,
        image_1: Union[Image, str],
        image_2: Optional[Union[Image, str]],
        aggregate: bool = False,
    ) -> Union[bool, Tuple[int, int, int], List[Tuple[bool, Tuple]]]:
        """
        Compares the state of a table in different images by materializing both tables into a temporary space
        and comparing them row-to-row.

        :param table_name: Name of the table.
        :param image_1: First image hash / object. If None, uses the state of the current staging area.
        :param image_2: Second image hash / object. If None, uses the state of the current staging area.
        :param aggregate: If True, returns a tuple of integers denoting added, removed and updated rows between
            the two images.
        :return: If the table doesn't exist in one of the images, returns True if it was added and False if it was
            removed. If `aggregate` is True, returns the aggregation of changes as specified before.
            Otherwise, returns a list of changes where each change is a tuple of
            `(True for added, False for removed, row contents)`.
        """

        if isinstance(image_1, str):
            image_1 = self.images.by_hash(image_1)
        if isinstance(image_2, str):
            image_2 = self.images.by_hash(image_2)

        # If the table doesn't exist in the first or the second image, short-circuit and
        # return the bool.
        if not table_exists_at(self, table_name, image_1):
            return True
        if not table_exists_at(self, table_name, image_2):
            return False

        # Special case: if diffing HEAD and staging (with aggregation), we can return that directly.
        if image_1 == self.head and image_2 is None and aggregate:
            return aggregate_changes(
                cast(
                    List[Tuple[int, int]],
                    self.object_engine.get_pending_changes(
                        self.to_schema(), table_name, aggregate=True
                    ),
                )
            )

        # If the table is the same in the two images, short circuit as well.
        if image_2 is not None:
            if set(image_1.get_table(table_name).objects) == set(
                image_2.get_table(table_name).objects
            ):
                return [] if not aggregate else (0, 0, 0)

        # Materialize both tables and compare them side-by-side.
        # TODO we can aggregate chunks in a similar way that LQ does it.
        return slow_diff(self, table_name, _hash(image_1), _hash(image_2), aggregate)


def import_table_from_remote(
    remote_repository: "Repository",
    remote_tables: List[str],
    remote_image_hash: str,
    target_repository: "Repository",
    target_tables: List[Any],
    target_hash: str = None,
) -> None:
    """
    Shorthand for importing one or more tables from a yet-uncloned remote. Here, the remote image hash is required,
    as otherwise we aren't necessarily able to determine what the remote head is.

    :param remote_repository: Remote Repository object
    :param remote_tables: List of remote tables to import
    :param remote_image_hash: Image hash to import the tables from
    :param target_repository: Target repository to import the tables to
    :param target_tables: Target table aliases
    :param target_hash: Hash of the image that's created with the import. Default random.
    """

    # In the future, we could do some vaguely intelligent interrogation of the remote to directly copy the required
    # metadata (object locations and relationships) into the local mountpoint. However, since the metadata is fairly
    # lightweight (we never download unneeded objects), we just clone it into a temporary mountpoint,
    # do the import into the target and destroy the temporary mp.
    tmp_mountpoint = Repository(
        namespace=remote_repository.namespace,
        repository=remote_repository.repository + "_clone_tmp",
    )

    clone(remote_repository, local_repository=tmp_mountpoint, download_all=False)
    target_repository.import_tables(
        target_tables,
        tmp_mountpoint,
        remote_tables,
        image_hash=remote_image_hash,
        target_hash=target_hash,
    )

    tmp_mountpoint.delete()
    target_repository.commit_engines()


def table_exists_at(
    repository: "Repository", table_name: str, image: Optional[Image] = None
) -> bool:
    """Determines whether a given table exists in a Splitgraph image without checking it out. If `image_hash` is None,
    determines whether the table exists in the current staging area."""
    if image is None:
        return repository.object_engine.table_exists(repository.to_schema(), table_name)
    else:
        try:
            image.get_table(table_name)
            return True
        except TableNotFoundError:
            return False


def _sync(
    target: "Repository",
    source: "Repository",
    download: bool = True,
    download_all: Optional[bool] = False,
    handler: str = "DB",
    handler_options: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Generic routine for syncing two repositories: fetches images, hashes, objects and tags
    on `source` that don't exist in `target`.

    Common code between push and pull, since the only difference between those routines is that
    uploading and downloading objects are different operations.

    :param target: Target Repository object
    :param source: Source Repository object
    :param download: If True, uses the download routines to download physical objects to self.
        If False, uses the upload routines to get `source` to upload physical objects to self / external.
    :param download_all: Whether to download all objects (pull option)
    :param handler: Upload handler
    :param handler_options: Upload handler options
    """
    if handler_options is None:
        handler_options = {}

    # Get the remote log and the list of objects we need to fetch.
    logging.info("Gathering remote metadata...")

    try:
        new_images, table_meta, object_locations, object_meta, tags = gather_sync_metadata(
            target, source
        )
        if not new_images:
            logging.info("Nothing to do.")
            return

        for image in new_images:
            target.images.add(
                image.parent_id,
                image.image_hash,
                image.created,
                image.comment,
                image.provenance_type,
                image.provenance_data,
            )

        if download:
            target.objects.register_objects(list(object_meta.values()))
            target.objects.register_object_locations(object_locations)
            # Don't actually download any real objects until the user tries to check out a revision, unless
            # they want to do it in advance.
            if download_all:
                logging.info("Fetching remote objects...")
                target.objects.download_objects(
                    source.objects,
                    objects_to_fetch=list(object_meta.keys()),
                    object_locations=object_locations,
                )

            # Don't check anything out, keep the repo bare.
            set_head(target, None)
        else:
            new_uploads = source.objects.upload_objects(
                target.objects,
                list(object_meta.keys()),
                handler=handler,
                handler_params=handler_options,
            )
            # Here we have to register the new objects after the upload but before we store their external
            # location (as the RLS for object_locations relies on the object metadata being in place)
            target.objects.register_objects(list(object_meta.values()), namespace=target.namespace)
            target.objects.register_object_locations(object_locations + new_uploads)
            source.objects.register_object_locations(new_uploads)

        # Register the new tables / tags.
        target.objects.register_tables(target, table_meta)
        target.set_tags(tags)
    except Exception:
        logging.exception("Error during repository sync")
        target.rollback_engines()
        source.rollback_engines()
        raise

    target.commit_engines()
    source.commit_engines()

    logging.info(
        "%s metadata for %d object(s), %d table version(s) and %d tag(s).",
        ("Fetched" if download else "Uploaded"),
        len(object_meta),
        len(table_meta),
        len([t for t in tags if t != "HEAD"]),
    )


def clone(
    remote_repository: Union["Repository", str],
    local_repository: Optional["Repository"] = None,
    download_all: Optional[bool] = False,
) -> "Repository":
    """
    Clones a remote Splitgraph repository or synchronizes remote changes with the local ones.

    If the target repository has no set upstream engine, the source repository becomes its upstream.

    :param remote_repository: Remote Repository object to clone or the repository's name. If a name is passed,
        the repository will be looked up on the current lookup path in order to find the engine the repository
        belongs to.
    :param local_repository: Local repository to clone into. If None, uses the same name as the remote.
    :param download_all: If True, downloads all objects and stores them locally. Otherwise, will only download required
        objects when a table is checked out.
    :return: A locally cloned Repository object.
    """
    if isinstance(remote_repository, str):
        remote_repository = lookup_repository(remote_repository, include_local=False)

    # Repository engine should be local by default
    if not local_repository:
        local_repository = Repository(remote_repository.namespace, remote_repository.repository)

    _sync(local_repository, remote_repository, download=True, download_all=download_all)

    if not local_repository.upstream:
        local_repository.upstream = remote_repository

    return local_repository


def _hash(image: Optional[Image]) -> Optional[str]:
    return image.image_hash if image is not None else None
