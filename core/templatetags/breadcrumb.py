from django import template

from core.models import Child

register = template.Library()


@register.inclusion_tag("core/child_quick_switch.html")
def child_quick_switch(current_child, target_url):
    # Only active children are offered for switching. An archived child's
    # own page stays reachable via its direct URL (and the child list).
    children = Child.active.exclude(slug=current_child.slug)

    return {
        "children": children,
        "current_child": current_child,
        "target_url": target_url,
    }
