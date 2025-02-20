#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
import json
import logging
from collections import defaultdict

from django.db import models
from django.forms import model_to_dict
from django.utils.translation import ugettext_lazy as _

from assets import const
from common.db.fields import EncryptMixin
from common.utils import lazyproperty
from orgs.mixins.models import OrgManager, JMSOrgBaseModel
from ..base import AbsConnectivity
from ..platform import Platform

__all__ = ['Asset', 'AssetQuerySet', 'default_node', 'Protocol']
logger = logging.getLogger(__name__)


def default_node():
    return []


class AssetManager(OrgManager):
    pass


class AssetQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True)

    def valid(self):
        return self.active()

    def has_protocol(self, name):
        return self.filter(protocols__contains=name)

    def group_by_platform(self) -> dict:
        groups = defaultdict(list)
        for asset in self.all():
            groups[asset.platform].append(asset)
        return groups


class NodesRelationMixin:
    NODES_CACHE_KEY = 'ASSET_NODES_{}'
    ALL_ASSET_NODES_CACHE_KEY = 'ALL_ASSETS_NODES'
    CACHE_TIME = 3600 * 24 * 7
    id: str
    _all_nodes_keys = None

    def get_nodes(self):
        from assets.models import Node
        nodes = self.nodes.all()
        if not nodes:
            nodes = Node.objects.filter(id=Node.org_root().id)
        return nodes

    def get_all_nodes(self, flat=False):
        from ..node import Node
        node_keys = self.get_all_node_keys()
        nodes = Node.objects.filter(key__in=node_keys).distinct()
        if not flat:
            return nodes
        node_ids = set(nodes.values_list('id', flat=True))
        return node_ids

    def get_all_node_keys(self):
        node_keys = set()
        for node in self.get_nodes():
            ancestor_keys = node.get_ancestor_keys(with_self=True)
            node_keys.update(ancestor_keys)
        return node_keys

    @classmethod
    def get_all_nodes_for_assets(cls, assets):
        from ..node import Node
        node_keys = set()
        for asset in assets:
            asset_node_keys = asset.get_all_node_keys()
            node_keys.update(asset_node_keys)
        nodes = Node.objects.filter(key__in=node_keys)
        return nodes


class Protocol(models.Model):
    name = models.CharField(max_length=32, verbose_name=_("Name"))
    port = models.IntegerField(verbose_name=_("Port"))
    asset = models.ForeignKey('Asset', on_delete=models.CASCADE, related_name='protocols', verbose_name=_("Asset"))
    _setting = None

    def __str__(self):
        return '{}/{}'.format(self.name, self.port)

    @lazyproperty
    def asset_platform_protocol(self):
        protocols = self.asset.platform.protocols.values('name', 'public', 'setting')
        protocols = list(filter(lambda p: p['name'] == self.name, protocols))
        return protocols[0] if len(protocols) > 0 else {}

    @property
    def setting(self):
        if self._setting is not None:
            return self._setting
        return self.asset_platform_protocol.get('setting', {})

    @setting.setter
    def setting(self, value):
        self._setting = value

    @property
    def public(self):
        return self.asset_platform_protocol.get('public', True)


class Asset(NodesRelationMixin, AbsConnectivity, JMSOrgBaseModel):
    Category = const.Category
    Type = const.AllTypes

    name = models.CharField(max_length=128, verbose_name=_('Name'))
    address = models.CharField(max_length=767, verbose_name=_('Address'), db_index=True)
    platform = models.ForeignKey(Platform, on_delete=models.PROTECT, verbose_name=_("Platform"), related_name='assets')
    domain = models.ForeignKey("assets.Domain", null=True, blank=True, related_name='assets',
                               verbose_name=_("Domain"), on_delete=models.SET_NULL)
    nodes = models.ManyToManyField('assets.Node', default=default_node, related_name='assets',
                                   verbose_name=_("Nodes"))
    is_active = models.BooleanField(default=True, verbose_name=_('Is active'))
    labels = models.ManyToManyField('assets.Label', blank=True, related_name='assets', verbose_name=_("Labels"))
    gathered_info = models.JSONField(verbose_name=_('Gathered info'), default=dict, blank=True)  # 资产的一些信息，如 硬件信息
    custom_info = models.JSONField(verbose_name=_('Custom info'), default=dict)

    objects = AssetManager.from_queryset(AssetQuerySet)()

    def __str__(self):
        return '{0.name}({0.address})'.format(self)

    @staticmethod
    def get_spec_values(instance, fields):
        info = {}
        for i in fields:
            v = getattr(instance, i.name)
            if isinstance(i, models.JSONField) and not isinstance(v, (list, dict)):
                v = json.loads(v)
            info[i.name] = v
        return info

    @lazyproperty
    def spec_info(self):
        instance = getattr(self, self.category, None)
        if not instance:
            return {}
        spec_fields = self.get_spec_fields(instance)
        return self.get_spec_values(instance, spec_fields)

    @staticmethod
    def get_spec_fields(instance, secret=False):
        spec_fields = [i for i in instance._meta.local_fields if i.name != 'asset_ptr']
        spec_fields = [i for i in spec_fields if isinstance(i, EncryptMixin) == secret]
        return spec_fields

    @lazyproperty
    def secret_info(self):
        instance = getattr(self, self.category, None)
        if not instance:
            return {}
        spec_fields = self.get_spec_fields(instance, secret=True)
        return self.get_spec_values(instance, spec_fields)

    @lazyproperty
    def info(self):
        info = {}
        info.update(self.gathered_info or {})
        info.update(self.custom_info or {})
        info.update(self.spec_info or {})
        return info

    @lazyproperty
    def auto_config(self):
        platform = self.platform
        automation = self.platform.automation
        auto_config = {
            'su_enabled': platform.su_enabled,
            'domain_enabled': platform.domain_enabled,
            'ansible_enabled': False
        }
        if not automation:
            return auto_config

        auto_config.update(model_to_dict(automation))
        return auto_config

    def get_target_ip(self):
        return self.address

    def get_target_ssh_port(self):
        protocol = self.protocols.all().filter(name='ssh').first()
        return protocol.port if protocol else 22

    @property
    def is_valid(self):
        warning = ''
        if not self.is_active:
            warning += ' inactive'
        if warning:
            return False, warning
        return True, warning

    def nodes_display(self):
        names = []
        for n in self.nodes.all():
            names.append(n.full_value)
        return names

    def labels_display(self):
        names = []
        for n in self.labels.all():
            names.append(n.name + ':' + n.value)
        return names

    @lazyproperty
    def type(self):
        return self.platform.type

    @lazyproperty
    def category(self):
        return self.platform.category

    def is_category(self, category):
        return self.category == category

    def is_type(self, tp):
        return self.type == tp

    @property
    def is_gateway(self):
        return self.platform.name == const.GATEWAY_NAME

    @lazyproperty
    def gateway(self):
        if not self.domain_id:
            return
        if not self.platform.domain_enabled:
            return
        return self.domain.select_gateway()

    def as_node(self):
        from assets.models import Node
        fake_node = Node()
        fake_node.id = self.id
        fake_node.key = self.id
        fake_node.value = self.name
        fake_node.asset = self
        fake_node.is_node = False
        return fake_node

    def as_tree_node(self, parent_node):
        from common.tree import TreeNode
        icon_skin = 'file'
        platform_type = self.platform.type.lower()
        if platform_type == 'windows':
            icon_skin = 'windows'
        elif platform_type == 'linux':
            icon_skin = 'linux'
        data = {
            'id': str(self.id),
            'name': self.name,
            'title': self.address,
            'pId': parent_node.key,
            'isParent': False,
            'open': False,
            'iconSkin': icon_skin,
            'meta': {
                'type': 'asset',
                'data': {
                    'id': self.id,
                    'name': self.name,
                    'address': self.address,
                    'protocols': self.protocols,
                }
            }
        }
        tree_node = TreeNode(**data)
        return tree_node

    @staticmethod
    def get_secret_type_assets(asset_ids, secret_type):
        assets = Asset.objects.filter(id__in=asset_ids)
        asset_protocol = assets.prefetch_related('protocols').values_list('id', 'protocols__name')
        protocol_secret_types_map = const.Protocol.protocol_secret_types()
        asset_secret_types_mapp = defaultdict(set)

        for asset_id, protocol in asset_protocol:
            secret_types = set(protocol_secret_types_map.get(protocol, []))
            asset_secret_types_mapp[asset_id].update(secret_types)

        return [
            asset for asset in assets
            if secret_type in asset_secret_types_mapp.get(asset.id, [])
        ]

    class Meta:
        unique_together = [('org_id', 'name')]
        verbose_name = _("Asset")
        ordering = ["name", ]
        permissions = [
            ('refresh_assethardwareinfo', _('Can refresh asset hardware info')),
            ('test_assetconnectivity', _('Can test asset connectivity')),
            ('match_asset', _('Can match asset')),
            ('change_assetnodes', _('Can change asset nodes')),
        ]
