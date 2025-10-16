"""
Document Storage Operations

Handles the storage and processing of crawled documents.
Extracted from crawl_orchestration_service.py for better modularity.
"""

import asyncio
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ...config.logfire_config import get_logger, safe_logfire_error, safe_logfire_info
from ..source_management_service import extract_source_summary, update_source_info
from ..storage.document_storage_service import add_documents_to_supabase
from ..storage.storage_services import DocumentStorageService
from .code_extraction_service import CodeExtractionService

logger = get_logger(__name__)


class DocumentStorageOperations:
    """
    Handles document storage operations for crawled content.
    """

    def __init__(self, supabase_client):
        """
        Initialize document storage operations.

        Args:
            supabase_client: The Supabase client for database operations
        """
        self.supabase_client = supabase_client
        self.doc_storage_service = DocumentStorageService(supabase_client)
        self.code_extraction_service = CodeExtractionService(supabase_client)
        self._project_root = self._determine_project_root()
        self._markdown_archive_dir = self._resolve_markdown_archive_dir()

    async def process_and_store_documents(
        self,
        crawl_results: list[dict],
        request: dict[str, Any],
        crawl_type: str,
        original_source_id: str,
        progress_callback: Callable | None = None,
        cancellation_check: Callable | None = None,
        source_url: str | None = None,
        source_display_name: str | None = None,
        url_to_page_id: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Process crawled documents and store them in the database.

        Args:
            crawl_results: List of crawled documents
            request: The original crawl request
            crawl_type: Type of crawl performed
            original_source_id: The source ID for all documents
            progress_callback: Optional callback for progress updates
            cancellation_check: Optional function to check for cancellation
            source_url: Optional original URL that was crawled
            source_display_name: Optional human-readable name for the source

        Returns:
            Dict containing storage statistics and document mappings
        """
        # Reuse initialized storage service for chunking
        storage_service = self.doc_storage_service

        # Prepare data for chunked storage
        all_urls = []
        all_chunk_numbers = []
        all_contents = []
        all_metadatas = []
        source_word_counts = {}
        url_to_full_document = {}
        processed_docs = 0

        # Process and chunk each document
        for doc_index, doc in enumerate(crawl_results):
            # Check for cancellation during document processing
            if cancellation_check:
                try:
                    cancellation_check()
                except asyncio.CancelledError:
                    if progress_callback:
                        await progress_callback(
                            "cancelled",
                            99,
                            f"Document processing cancelled at document {doc_index + 1}/{len(crawl_results)}"
                        )
                    raise

            doc_url = (doc.get('url') or '').strip()
            markdown_content = (doc.get('markdown') or '').strip()

            # Skip documents with empty or whitespace-only content or missing URLs
            if not markdown_content or not doc_url:
                logger.debug(f"Skipping document {doc_index}: empty {'URL' if not doc_url else 'content'}")
                continue

            # Increment processed document count
            processed_docs += 1

            # Store full document for code extraction context
            url_to_full_document[doc_url] = markdown_content

            # CHUNK THE CONTENT
            chunks = await storage_service.smart_chunk_text_async(markdown_content, chunk_size=5000)

            # Use the original source_id for all documents
            source_id = original_source_id
            safe_logfire_info(f"Using original source_id '{source_id}' for URL '{doc_url}'")

            # Process each chunk
            for i, chunk in enumerate(chunks):
                # Check for cancellation during chunk processing
                if cancellation_check and i % 10 == 0:  # Check every 10 chunks
                    try:
                        cancellation_check()
                    except asyncio.CancelledError:
                        if progress_callback:
                            await progress_callback(
                                "cancelled",
                                99,
                                f"Chunk processing cancelled at chunk {i + 1}/{len(chunks)} of document {doc_index + 1}"
                            )
                        raise

                all_urls.append(doc_url)
                all_chunk_numbers.append(i)
                all_contents.append(chunk)

                # Create metadata for each chunk (page_id will be set later)
                word_count = len(chunk.split())
                metadata = {
                    "url": doc_url,
                    "title": doc.get("title", ""),
                    "description": doc.get("description", ""),
                    "source_id": source_id,
                    "knowledge_type": request.get("knowledge_type", "documentation"),
                    "page_id": None,  # Will be set after pages are stored
                    "crawl_type": crawl_type,
                    "word_count": word_count,
                    "char_count": len(chunk),
                    "chunk_index": i,
                    "tags": request.get("tags", []),
                }
                all_metadatas.append(metadata)

                # Accumulate word count
                source_word_counts[source_id] = source_word_counts.get(source_id, 0) + word_count

                # Yield control every 10 chunks to prevent event loop blocking
                if i > 0 and i % 10 == 0:
                    await asyncio.sleep(0)

            # Yield control after processing each document
            if doc_index > 0 and doc_index % 5 == 0:
                await asyncio.sleep(0)

        # Create/update source record FIRST (required for FK constraints on pages and chunks)
        if all_contents and all_metadatas:
            await self._create_source_records(
                all_metadatas, all_contents, source_word_counts, request,
                source_url, source_display_name
            )

        # Store pages AFTER source is created but BEFORE chunks (FK constraint requirement)
        from .page_storage_operations import PageStorageOperations
        page_storage_ops = PageStorageOperations(self.supabase_client)

        # Check if this is an llms-full.txt file
        is_llms_full = crawl_type == "llms-txt" or (
            len(url_to_full_document) == 1 and
            next(iter(url_to_full_document.keys())).endswith("llms-full.txt")
        )

        if is_llms_full and url_to_full_document:
            # Handle llms-full.txt with section-based pages
            base_url = next(iter(url_to_full_document.keys()))
            content = url_to_full_document[base_url]

            # Store section pages
            url_to_page_id = await page_storage_ops.store_llms_full_sections(
                base_url,
                content,
                original_source_id,
                request,
                crawl_type="llms_full",
            )

            # Parse sections and re-chunk each section
            from .helpers.llms_full_parser import parse_llms_full_sections
            sections = parse_llms_full_sections(content, base_url)

            # Clear existing chunks and re-create from sections
            all_urls.clear()
            all_chunk_numbers.clear()
            all_contents.clear()
            all_metadatas.clear()
            url_to_full_document.clear()

            # Chunk each section separately
            for section in sections:
                # Update url_to_full_document with section content
                url_to_full_document[section.url] = section.content
                section_chunks = await storage_service.smart_chunk_text_async(
                    section.content, chunk_size=5000
                )

                for i, chunk in enumerate(section_chunks):
                    all_urls.append(section.url)
                    all_chunk_numbers.append(i)
                    all_contents.append(chunk)

                    word_count = len(chunk.split())
                    metadata = {
                        "url": section.url,
                        "title": section.section_title,
                        "description": "",
                        "source_id": original_source_id,
                        "knowledge_type": request.get("knowledge_type", "documentation"),
                        "page_id": url_to_page_id.get(section.url),
                        "crawl_type": "llms_full",
                        "word_count": word_count,
                        "char_count": len(chunk),
                        "chunk_index": i,
                        "tags": request.get("tags", []),
                    }
                    all_metadatas.append(metadata)
        else:
            # Handle regular pages
            reconstructed_crawl_results = []
            for url, markdown in url_to_full_document.items():
                reconstructed_crawl_results.append({
                    "url": url,
                    "markdown": markdown,
                })

            if reconstructed_crawl_results:
                url_to_page_id = await page_storage_ops.store_pages(
                    reconstructed_crawl_results,
                    original_source_id,
                    request,
                    crawl_type,
                )
            else:
                url_to_page_id = {}

            # Update all chunk metadata with correct page_id
            for metadata in all_metadatas:
                chunk_url = metadata.get("url")
                if chunk_url and chunk_url in url_to_page_id:
                    metadata["page_id"] = url_to_page_id[chunk_url]

        safe_logfire_info(f"url_to_full_document keys: {list(url_to_full_document.keys())[:5]}")

        # Log chunking results
        avg_chunks = (len(all_contents) / processed_docs) if processed_docs > 0 else 0.0
        safe_logfire_info(
            f"Document storage | processed={processed_docs}/{len(crawl_results)} | chunks={len(all_contents)} | avg_chunks_per_doc={avg_chunks:.1f}"
        )

        # Call add_documents_to_supabase with the correct parameters
        storage_stats = await add_documents_to_supabase(
            client=self.supabase_client,
            urls=all_urls,  # Now has entry per chunk
            chunk_numbers=all_chunk_numbers,  # Proper chunk numbers (0, 1, 2, etc)
            contents=all_contents,  # Individual chunks
            metadatas=all_metadatas,  # Metadata per chunk
            url_to_full_document=url_to_full_document,
            batch_size=25,  # Increased from 10 for better performance
            progress_callback=progress_callback,  # Pass the callback for progress updates
            enable_parallel_batches=True,  # Enable parallel processing
            provider=None,  # Use configured provider
            cancellation_check=cancellation_check,  # Pass cancellation check
            url_to_page_id=url_to_page_id,  # Link chunks to pages
        )

        # Calculate chunk counts
        chunk_count = len(all_contents)
        chunks_stored = storage_stats.get("chunks_stored", 0)

        return {
            'chunk_count': chunk_count,
            'chunks_stored': chunks_stored,
            'total_word_count': sum(source_word_counts.values()),
            'url_to_full_document': url_to_full_document,
            'source_id': original_source_id,
            'markdown_files_saved': await self._persist_markdown_archive(
                url_to_full_document,
                original_source_id,
            ),
        }

    def _determine_project_root(self) -> Path:
        """Locate the repository root for fallback storage."""
        try:
            return Path(__file__).resolve().parents[5]
        except IndexError:
            return Path.cwd()

    def _resolve_markdown_archive_dir(self) -> Path:
        """
        Determine where scraped markdown archives should be stored.

        Returns:
            Absolute path to the archive directory.
        """
        configured = os.getenv("ARCHON_MARKDOWN_ARCHIVE_DIR")
        project_root = self._project_root

        if configured:
            archive_dir = Path(configured).expanduser()
            if not archive_dir.is_absolute():
                archive_dir = (project_root / archive_dir).resolve()
        else:
            archive_dir = Path("/Data_dir").resolve()

        return archive_dir

    async def _persist_markdown_archive(
        self,
        url_to_full_document: dict[str, str],
        source_id: str,
    ) -> int:
        """
        Persist full markdown documents locally for easier access outside the vector store.

        Args:
            url_to_full_document: Mapping of crawled URLs to full markdown text.
            source_id: Source identifier used to group archived files.

        Returns:
            Number of markdown files written successfully.
        """
        if not url_to_full_document:
            return 0

        sanitized_source_id = self._sanitize_source_id(source_id)

        # Ensure parent directory exists lazily to avoid churn when disabled.
        archive_root = self._markdown_archive_dir / sanitized_source_id

        try:
            archive_root.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            safe_logfire_error(
                f"Failed to create markdown archive directory '{archive_root}': {exc}"
            )
            fallback_base = (self._project_root / "data" / "scraped_markdown").resolve()
            try:
                fallback_base.mkdir(parents=True, exist_ok=True)
                archive_root = fallback_base / sanitized_source_id
                archive_root.mkdir(parents=True, exist_ok=True)
                safe_logfire_info(
                    f"Using fallback markdown archive directory '{archive_root}'"
                )
            except Exception as fallback_exc:
                safe_logfire_error(
                    f"Failed to create fallback markdown archive directory '{fallback_base}': {fallback_exc}"
                )
                return 0

        async def write_file(url: str, markdown: str) -> bool:
            if not markdown:
                return False

            filename = f"{self._slugify_url(url)}.md"
            target_path = archive_root / filename

            try:
                # Offload blocking IO to thread to avoid tying up the event loop.
                await asyncio.to_thread(
                    target_path.write_text,
                    markdown,
                    encoding="utf-8",
                )
                return True
            except Exception as exc:
                safe_logfire_error(
                    f"Failed to write markdown archive for '{url}' to '{target_path}': {exc}"
                )
                return False

        write_tasks = [
            write_file(url, markdown)
            for url, markdown in url_to_full_document.items()
            if markdown and url
        ]

        if not write_tasks:
            return 0

        results = await asyncio.gather(*write_tasks, return_exceptions=True)
        return sum(1 for result in results if result is True)

    @staticmethod
    def _sanitize_source_id(source_id: str) -> str:
        """Create a filesystem-safe directory name from the source ID."""
        if not source_id:
            return "unknown-source"
        sanitized = re.sub(r"[^a-zA-Z0-9._-]", "-", source_id)
        sanitized = re.sub(r"-{2,}", "-", sanitized).strip("-")
        return sanitized or "unknown-source"

    @staticmethod
    def _slugify_url(url: str) -> str:
        """Generate a stable filename slug from a document URL."""
        parsed = urlparse(url)
        parts = [parsed.netloc or "document"]
        if parsed.path and parsed.path != "/":
            parts.append(parsed.path.strip("/").replace("/", "_"))
        if parsed.fragment:
            parts.append(parsed.fragment.replace("/", "_"))
        base = "_".join(filter(None, parts)) or "document"
        base = re.sub(r"[^a-zA-Z0-9._-]", "-", base)
        base = re.sub(r"-{2,}", "-", base).strip("-")
        return base or "document"

    async def _create_source_records(
        self,
        all_metadatas: list[dict],
        all_contents: list[str],
        source_word_counts: dict[str, int],
        request: dict[str, Any],
        source_url: str | None = None,
        source_display_name: str | None = None,
    ):
        """
        Create or update source records in the database.

        Args:
            all_metadatas: List of metadata for all chunks
            all_contents: List of all chunk contents
            source_word_counts: Word counts per source_id
            request: Original crawl request
        """
        # Find ALL unique source_ids in the crawl results
        unique_source_ids = set()
        source_id_contents = {}
        source_id_word_counts = {}

        for i, metadata in enumerate(all_metadatas):
            source_id = metadata["source_id"]
            unique_source_ids.add(source_id)

            # Group content by source_id for better summaries
            if source_id not in source_id_contents:
                source_id_contents[source_id] = []
            source_id_contents[source_id].append(all_contents[i])

            # Track word counts per source_id
            if source_id not in source_id_word_counts:
                source_id_word_counts[source_id] = 0
            source_id_word_counts[source_id] += metadata.get('word_count', 0)

        safe_logfire_info(
            f"Found {len(unique_source_ids)} unique source_ids: {list(unique_source_ids)}"
        )

        # Create source records for ALL unique source_ids
        for source_id in unique_source_ids:
            # Get combined content for this specific source_id
            source_contents = source_id_contents[source_id]
            combined_content = ""
            for chunk in source_contents[:3]:  # First 3 chunks for this source
                if len(combined_content) + len(chunk) < 15000:
                    combined_content += " " + chunk
                else:
                    break

            # Generate summary with fallback
            try:
                # Call async extract_source_summary directly
                summary = await extract_source_summary(source_id, combined_content)
            except Exception as e:
                logger.error(f"Failed to generate AI summary for '{source_id}'", exc_info=True)
                safe_logfire_error(
                    f"Failed to generate AI summary for '{source_id}': {str(e)}, using fallback"
                )
                # Fallback to simple summary
                summary = f"Documentation from {source_id} - {len(source_contents)} pages crawled"

            # Update source info in database BEFORE storing documents
            safe_logfire_info(
                f"About to create/update source record for '{source_id}' (word count: {source_id_word_counts[source_id]})"
            )
            try:
                # Call async update_source_info directly
                await update_source_info(
                    client=self.supabase_client,
                    source_id=source_id,
                    summary=summary,
                    word_count=source_id_word_counts[source_id],
                    content=combined_content,
                    knowledge_type=request.get("knowledge_type", "documentation"),
                    tags=request.get("tags", []),
                    update_frequency=0,  # Set to 0 since we're using manual refresh
                    original_url=request.get("url"),  # Store the original crawl URL
                    source_url=source_url,
                    source_display_name=source_display_name,
                )
                safe_logfire_info(f"Successfully created/updated source record for '{source_id}'")
            except Exception as e:
                logger.error(f"Failed to create/update source record for '{source_id}'", exc_info=True)
                safe_logfire_error(
                    f"Failed to create/update source record for '{source_id}': {str(e)}"
                )
                # Try a simpler approach with minimal data
                try:
                    safe_logfire_info(f"Attempting fallback source creation for '{source_id}'")
                    fallback_data = {
                        "source_id": source_id,
                        "title": source_id,  # Use source_id as title fallback
                        "summary": summary,
                        "total_word_count": source_id_word_counts[source_id],
                        "metadata": {
                            "knowledge_type": request.get("knowledge_type", "documentation"),
                            "tags": request.get("tags", []),
                            "auto_generated": True,
                            "fallback_creation": True,
                            "original_url": request.get("url"),
                        },
                    }

                    # Add new fields if provided
                    if source_url:
                        fallback_data["source_url"] = source_url
                    if source_display_name:
                        fallback_data["source_display_name"] = source_display_name

                    self.supabase_client.table("archon_sources").upsert(fallback_data).execute()
                    safe_logfire_info(f"Fallback source creation succeeded for '{source_id}'")
                except Exception as fallback_error:
                    logger.error(f"Both source creation attempts failed for '{source_id}'", exc_info=True)
                    safe_logfire_error(
                        f"Both source creation attempts failed for '{source_id}': {str(fallback_error)}"
                    )
                    raise RuntimeError(
                        f"Unable to create source record for '{source_id}'. This will cause foreign key violations."
                    ) from fallback_error

        # Verify ALL source records exist before proceeding with document storage
        if unique_source_ids:
            for source_id in unique_source_ids:
                try:
                    source_check = (
                        self.supabase_client.table("archon_sources")
                        .select("source_id")
                        .eq("source_id", source_id)
                        .execute()
                    )
                    if not source_check.data:
                        raise Exception(
                            f"Source record verification failed - '{source_id}' does not exist in sources table"
                        )
                    safe_logfire_info(f"Source record verified for '{source_id}'")
                except Exception as e:
                    logger.error(f"Source verification failed for '{source_id}'", exc_info=True)
                    safe_logfire_error(f"Source verification failed for '{source_id}': {str(e)}")
                    raise

            safe_logfire_info(
                f"All {len(unique_source_ids)} source records verified - proceeding with document storage"
            )

    async def extract_and_store_code_examples(
        self,
        crawl_results: list[dict],
        url_to_full_document: dict[str, str],
        source_id: str,
        progress_callback: Callable | None = None,
        cancellation_check: Callable[[], None] | None = None,
        provider: str | None = None,
        embedding_provider: str | None = None,
    ) -> int:
        """
        Extract code examples from crawled documents and store them.

        Args:
            crawl_results: List of crawled documents
            url_to_full_document: Mapping of URLs to full document content
            source_id: The unique source_id for all documents
            progress_callback: Optional callback for progress updates
            cancellation_check: Optional function to check for cancellation
            provider: Optional LLM provider to use for code summaries
            embedding_provider: Optional embedding provider override for code example embeddings

        Returns:
            Number of code examples stored
        """
        result = await self.code_extraction_service.extract_and_store_code_examples(
            crawl_results,
            url_to_full_document,
            source_id,
            progress_callback,
            cancellation_check,
            provider,
            embedding_provider,
        )

        return result
