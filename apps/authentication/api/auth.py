# -*- coding: utf-8 -*-
#
import uuid
import time

from django.core.cache import cache
from django.urls import reverse
from django.shortcuts import get_object_or_404
from django.utils.translation import ugettext as _
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.generics import CreateAPIView
from rest_framework.views import APIView

from common.utils import get_logger, get_request_ip, get_object_or_none
from common.permissions import IsOrgAdminOrAppUser, IsValidUser
from orgs.mixins.api import RootOrgViewMixin
from users.serializers import UserSerializer
from users.models import User
from assets.models import Asset, SystemUser
from users.utils import (
    check_otp_code, increase_login_failed_count,
    is_block_login, clean_failed_count
)
from .. import const
from ..utils import check_user_valid
from ..serializers import OtpVerifySerializer
from ..signals import post_auth_success, post_auth_failed

logger = get_logger(__name__)
__all__ = [
    'UserAuthApi', 'UserConnectionTokenApi', 'UserOtpAuthApi',
    'UserOtpVerifyApi', 'UserOrderAcceptAuthApi',
]


class UserAuthApi(RootOrgViewMixin, APIView):
    permission_classes = (AllowAny,)
    serializer_class = UserSerializer

    def get_serializer_context(self):
        return {
            'request': self.request,
            'view': self
        }

    def get_serializer(self, *args, **kwargs):
        kwargs['context'] = self.get_serializer_context()
        return self.serializer_class(*args, **kwargs)

    def post(self, request):
        # limit login
        username = request.data.get('username')
        ip = request.data.get('remote_addr', None)
        ip = ip or get_request_ip(request)

        if is_block_login(username, ip):
            msg = _("Log in frequently and try again later")
            logger.warn(msg + ': ' + username + ':' + ip)
            return Response({'msg': msg}, status=401)

        user, msg = self.check_user_valid(request)
        if not user:
            username = request.data.get('username', '')
            self.send_auth_signal(success=False, username=username, reason=msg)
            increase_login_failed_count(username, ip)
            return Response({'msg': msg}, status=401)

        if not user.otp_enabled:
            self.send_auth_signal(success=True, user=user)
            # 登陆成功，清除原来的缓存计数
            clean_failed_count(username, ip)
            token, expired_at = user.create_bearer_token(request)
            return Response(
                {'token': token, 'user': self.get_serializer(user).data}
            )

        seed = uuid.uuid4().hex
        cache.set(seed, user, 300)
        return Response(
            {
                'code': 101,
                'msg': _('Please carry seed value and '
                         'conduct MFA secondary certification'),
                'otp_url': reverse('api-auth:user-otp-auth'),
                'seed': seed,
                'user': self.get_serializer(user).data
            }, status=300
        )

    @staticmethod
    def check_user_valid(request):
        username = request.data.get('username', '')
        password = request.data.get('password', '')
        public_key = request.data.get('public_key', '')
        user, msg = check_user_valid(
            username=username, password=password,
            public_key=public_key
        )
        return user, msg

    def send_auth_signal(self, success=True, user=None, username='', reason=''):
        if success:
            post_auth_success.send(sender=self.__class__, user=user, request=self.request)
        else:
            post_auth_failed.send(
                sender=self.__class__, username=username,
                request=self.request, reason=reason
            )


class UserConnectionTokenApi(RootOrgViewMixin, APIView):
    permission_classes = (IsOrgAdminOrAppUser,)

    def post(self, request):
        user_id = request.data.get('user', '')
        asset_id = request.data.get('asset', '')
        system_user_id = request.data.get('system_user', '')
        token = str(uuid.uuid4())
        user = get_object_or_404(User, id=user_id)
        asset = get_object_or_404(Asset, id=asset_id)
        system_user = get_object_or_404(SystemUser, id=system_user_id)
        value = {
            'user': user_id,
            'username': user.username,
            'asset': asset_id,
            'hostname': asset.hostname,
            'system_user': system_user_id,
            'system_user_name': system_user.name
        }
        cache.set(token, value, timeout=20)
        return Response({"token": token}, status=201)

    def get(self, request):
        token = request.query_params.get('token')
        user_only = request.query_params.get('user-only', None)
        value = cache.get(token, None)

        if not value:
            return Response('', status=404)

        if not user_only:
            return Response(value)
        else:
            return Response({'user': value['user']})

    def get_permissions(self):
        if self.request.query_params.get('user-only', None):
            self.permission_classes = (AllowAny,)
        return super().get_permissions()


class UserOtpAuthApi(RootOrgViewMixin, APIView):
    permission_classes = (AllowAny,)
    serializer_class = UserSerializer

    def get_serializer_context(self):
        return {
            'request': self.request,
            'view': self
        }

    def get_serializer(self, *args, **kwargs):
        kwargs['context'] = self.get_serializer_context()
        return self.serializer_class(*args, **kwargs)

    def post(self, request):
        otp_code = request.data.get('otp_code', '')
        seed = request.data.get('seed', '')
        user = cache.get(seed, None)
        if not user:
            return Response(
                {'msg': _('Please verify the user name and password first')},
                status=401
            )
        if not check_otp_code(user.otp_secret_key, otp_code):
            self.send_auth_signal(success=False, username=user.username, reason=const.mfa_failed)
            return Response({'msg': _('MFA certification failed')}, status=401)
        self.send_auth_signal(success=True, user=user)
        token, expired_at = user.create_bearer_token(request)
        data = {'token': token, 'user': self.get_serializer(user).data}
        return Response(data)

    def send_auth_signal(self, success=True, user=None, username='', reason=''):
        if success:
            post_auth_success.send(sender=self.__class__, user=user, request=self.request)
        else:
            post_auth_failed.send(
                sender=self.__class__, username=username,
                request=self.request, reason=reason
            )


class UserOtpVerifyApi(CreateAPIView):
    permission_classes = (IsValidUser,)
    serializer_class = OtpVerifySerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        code = serializer.validated_data["code"]

        if request.user.check_otp(code):
            request.session["MFA_VERIFY_TIME"] = int(time.time())
            return Response({"ok": "1"})
        else:
            return Response({"error": "Code not valid"}, status=400)


class UserOrderAcceptAuthApi(APIView):
    permission_classes = ()

    def get(self, request, *args, **kwargs):
        from orders.models import LoginConfirmOrder
        order_id = self.request.session.get("auth_order_id")
        logger.debug('Login confirm order id: {}'.format(order_id))
        if not order_id:
            order = None
        else:
            order = get_object_or_none(LoginConfirmOrder, pk=order_id)
        if not order:
            error = _("No order found or order expired")
            return Response({"error": error, "status": "not found"}, status=404)
        if order.status == order.STATUS_ACCEPTED:
            self.request.session["auth_confirm"] = "1"
            return Response({"msg": "ok"})
        elif order.status == order.STATUS_REJECTED:
            error = _("Order was rejected by {}").format(order.assignee_display)
        else:
            error = "Order status: {}".format(order.status)
        return Response({"error": error, "status": order.status}, status=400)
