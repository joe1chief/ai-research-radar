"""Collector registry."""

from .arxiv import ArxivCollector
from .arxiv_oai import ArxivOAICollector
from .base import BaseCollector, UnsupportedCollectorError
from .conference import ACLAnthologyCollector, PMLRCollector
from .cninfo import CNInfoAnnouncementsCollector
from .github import GitHubReleaseCollector
from .html import HtmlListingCollector
from .huggingface import HuggingFaceModelsCollector
from .rss import RSSCollector
from .samr import SAMRStandardsCollector
from .openreview import OpenReviewCollector
from .sec import SECSubmissionsCollector
from .sitemap import SitemapCollector
from .sse import SSEAnnouncementsCollector

__all__ = [
    "ArxivCollector",
    "ArxivOAICollector",
    "BaseCollector",
    "ACLAnthologyCollector",
    "PMLRCollector",
    "CNInfoAnnouncementsCollector",
    "OpenReviewCollector",
    "GitHubReleaseCollector",
    "HtmlListingCollector",
    "HuggingFaceModelsCollector",
    "RSSCollector",
    "SAMRStandardsCollector",
    "SECSubmissionsCollector",
    "SitemapCollector",
    "SSEAnnouncementsCollector",
    "UnsupportedCollectorError",
    "collector_for",
]


def collector_for(spec, **kwargs):
    by_kind = {
        "arxiv_api": ArxivCollector,
        "arxiv_oai": ArxivOAICollector,
        "openreview_api": OpenReviewCollector,
        "acl_anthology": ACLAnthologyCollector,
        "pmlr": PMLRCollector,
        "rss": RSSCollector,
        "samr_standards": SAMRStandardsCollector,
        "github_releases": GitHubReleaseCollector,
        "html": HtmlListingCollector,
        "huggingface_models": HuggingFaceModelsCollector,
        "sec_submissions": SECSubmissionsCollector,
        "sitemap": SitemapCollector,
        "sse_announcements": SSEAnnouncementsCollector,
        "cninfo_announcements": CNInfoAnnouncementsCollector,
    }
    cls = by_kind.get(spec.kind)
    if cls is None:
        raise UnsupportedCollectorError(f"unsupported collector kind: {spec.kind}")
    return cls(spec, **kwargs)
