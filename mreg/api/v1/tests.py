from datetime import timedelta

from django.contrib.auth import get_user_model
from django.conf import settings
from django.contrib.auth.models import Group
from django.test import TestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient, APITestCase

from mreg.models import (Cname, HinfoPreset, Host, Ipaddress, NameServer,
                         Naptr, PtrOverride, Srv, Network, Txt, ForwardZone,
                         ReverseZone, ModelChangeLog, Sshfp)

from mreg.utils import create_serialno


class MissingSettings(Exception):
    pass


class MregAPITestCase(APITestCase):

    def setUp(self):
        self.client = self.get_token_client()

    def get_token_client(self, add_groups=True):
        self.user, created = get_user_model().objects.get_or_create(username='nobody')
        token, created = Token.objects.get_or_create(user=self.user)
        self.add_user_to_groups('REQUIRED_USER_GROUPS')
        if add_groups:
            self.add_user_to_groups('SUPERUSER_GROUP')
            self.add_user_to_groups('ADMINUSER_GROUP')
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION='Token ' + token.key)
        return client

    def add_user_to_groups(self, group_setting_name):
        groups = getattr(settings, group_setting_name, None)
        if groups is None:
            raise MissingSettings(f"{group_setting_name} not set")
        if not isinstance(groups, (list, tuple)):
            groups = (groups, )
        for groupname in groups:
            group, created = Group.objects.get_or_create(name=groupname)
            group.user_set.add(self.user)
            group.save()


def clean_and_save(entity):
    entity.full_clean()
    entity.save()


class APITokenAutheticationTestCase(MregAPITestCase):
    """Test various token authentication operations."""

    def test_logout(self):
        ret = self.client.get("/zones/")
        self.assertEqual(ret.status_code, 200)
        ret = self.client.post("/api/token-logout/")
        self.assertEqual(ret.status_code, 200)
        ret = self.client.get("/zones/")
        self.assertEqual(ret.status_code, 401)

    def test_force_expire(self):
        ret = self.client.get("/zones/")
        self.assertEqual(ret.status_code, 200)
        user = get_user_model().objects.get(username='nobody')
        token = Token.objects.get(user=user)
        EXPIRE_HOURS = getattr(settings, 'REST_FRAMEWORK_TOKEN_EXPIRE_HOURS', 8)
        token.created = timezone.now() - timedelta(hours=EXPIRE_HOURS)
        token.save()
        ret = self.client.get("/zones/")
        self.assertEqual(ret.status_code, 401)


class APIAutoupdateZonesTestCase(MregAPITestCase):
    """This class tests the autoupdate of zones' updated_at whenever
       various models are added/deleted/renamed/changed etc."""

    def setUp(self):
        """Add the a couple of zones and hosts for used in testing."""
        super().setUp()
        self.host1 = {"name": "host1.example.org",
                      "ipaddress": "10.10.0.1",
                      "contact": "mail@example.org"}
        self.delegation = {"name": "delegated.example.org",
                           "nameservers": "ns.example.org"}
        self.subzone = {"name": "sub.example.org",
                        "email": "hostmaster@example.org",
                        "primary_ns": "ns.example.org"}
        self.zone_exampleorg = ForwardZone(name='example.org',
                                           primary_ns='ns.example.org',
                                           email='hostmaster@example.org')
        self.zone_examplecom = ForwardZone(name='example.com',
                                           primary_ns='ns.example.com',
                                           email='hostmaster@example.com')
        self.zone_1010 = ReverseZone(name='10.10.in-addr.arpa',
                                     primary_ns='ns.example.org',
                                     email='hostmaster@example.org')
        clean_and_save(self.zone_exampleorg)
        clean_and_save(self.zone_examplecom)
        clean_and_save(self.zone_1010)

    def test_add_host(self):
        old_org_updated_at = self.zone_exampleorg.updated_at
        old_1010_updated_at = self.zone_1010.updated_at
        self.client.post('/hosts/', self.host1)
        self.zone_exampleorg.refresh_from_db()
        self.zone_1010.refresh_from_db()
        self.assertTrue(self.zone_exampleorg.updated)
        self.assertTrue(self.zone_1010.updated)
        self.assertGreater(self.zone_exampleorg.updated_at, old_org_updated_at)
        self.assertGreater(self.zone_1010.updated_at, old_1010_updated_at)

    def test_rename_host(self):
        self.client.post('/hosts/', self.host1)
        self.zone_exampleorg.refresh_from_db()
        self.zone_examplecom.refresh_from_db()
        self.zone_1010.refresh_from_db()
        old_org_updated_at = self.zone_exampleorg.updated_at
        old_com_updated_at = self.zone_examplecom.updated_at
        old_1010_updated_at = self.zone_1010.updated_at
        self.client.patch('/hosts/host1.example.org',
                          {"name": "host1.example.com"})
        self.zone_exampleorg.refresh_from_db()
        self.zone_examplecom.refresh_from_db()
        self.zone_1010.refresh_from_db()
        self.assertTrue(self.zone_examplecom.updated)
        self.assertTrue(self.zone_exampleorg.updated)
        self.assertTrue(self.zone_1010.updated)
        self.assertGreater(self.zone_examplecom.updated_at, old_com_updated_at)
        self.assertGreater(self.zone_exampleorg.updated_at, old_org_updated_at)
        self.assertGreater(self.zone_1010.updated_at, old_1010_updated_at)

    def test_change_soa(self):
        self.zone_exampleorg.updated = False
        self.zone_exampleorg.save()
        ret = self.client.patch('/zones/example.org', {'ttl': 1000})
        self.assertEqual(ret.status_code, 204)
        self.zone_exampleorg.refresh_from_db()
        self.assertTrue(self.zone_exampleorg.updated)

    def test_changed_nameservers(self):
        self.zone_exampleorg.updated = False
        self.zone_exampleorg.save()
        ret = self.client.patch('/zones/example.org/nameservers',
                                {'primary_ns': 'ns2.example.org'})
        self.assertEqual(ret.status_code, 204)
        self.zone_exampleorg.refresh_from_db()
        self.assertTrue(self.zone_exampleorg.updated)

    def test_added_subzone(self):
        self.zone_exampleorg.updated = False
        self.zone_exampleorg.save()
        self.client.post("/zones/", self.subzone)
        self.zone_exampleorg.refresh_from_db()
        self.assertTrue(self.zone_exampleorg.updated)

    def test_removed_subzone(self):
        self.client.post("/zones/", self.subzone)
        self.zone_exampleorg.updated = False
        self.zone_exampleorg.save()
        self.client.delete("/zones/sub.example.org")
        self.zone_exampleorg.refresh_from_db()
        self.assertTrue(self.zone_exampleorg.updated)

    def test_add_delegation(self):
        self.zone_exampleorg.updated = False
        self.zone_exampleorg.save()
        ret = self.client.post("/zones/example.org/delegations/", self.delegation)
        self.assertEqual(ret.status_code, 201)
        self.zone_exampleorg.refresh_from_db()
        self.assertTrue(self.zone_exampleorg.updated)

    def test_remove_delegation(self):
        self.client.post("/zones/example.org/delegations/", self.delegation)
        self.zone_exampleorg.updated = False
        self.zone_exampleorg.save()
        self.client.delete("/zones/example.org/delegations/delegated.example.org")
        self.zone_exampleorg.refresh_from_db()
        self.assertTrue(self.zone_exampleorg.updated)


class APIAutoupdateHostZoneTestCase(MregAPITestCase):
    """This class tests that a Host's zone attribute is correct and updated
       when renaming etc.
       """

    def setUp(self):
        """Add the a couple of zones and hosts for used in testing."""
        super().setUp()
        self.zone_org = ForwardZone(name='example.org',
                                    primary_ns='ns.example.org',
                                    email='hostmaster@example.org')
        self.zone_long = ForwardZone(name='longexample.org',
                                     primary_ns='ns.example.org',
                                     email='hostmaster@example.org')
        self.zone_sub = ForwardZone(name='sub.example.org',
                                    primary_ns='ns.example.org',
                                    email='hostmaster@example.org')
        self.zone_com = ForwardZone(name='example.com',
                                    primary_ns='ns.example.com',
                                    email='hostmaster@example.com')
        self.zone_1010 = ReverseZone(name='10.10.in-addr.arpa',
                                     primary_ns='ns.example.org',
                                     email='hostmaster@example.org')

        self.org_host1 = {"name": "host1.example.org",
                         "ipaddress": "10.10.0.1",
                         "contact": "mail@example.org"}
        self.org_host2 = {"name": "example.org",
                          "ipaddress": "10.10.0.2",
                          "contact": "mail@example.org"}
        self.sub_host1 = {"name": "host1.sub.example.org",
                          "ipaddress": "10.20.0.1",
                          "contact": "mail@example.org"}
        self.sub_host2 = {"name": "sub.example.org",
                          "ipaddress": "10.20.0.1",
                          "contact": "mail@example.org"}
        self.long_host1 = {"name": "host1.longexample.org",
                           "ipaddress": "10.30.0.1",
                           "contact": "mail@example.org"}
        self.long_host2 = {"name": "longexample.org",
                           "ipaddress": "10.30.0.2",
                           "contact": "mail@example.org"}
        clean_and_save(self.zone_org)
        clean_and_save(self.zone_long)
        clean_and_save(self.zone_com)
        clean_and_save(self.zone_sub)
        clean_and_save(self.zone_1010)

    def test_add_host_known_zone(self):
        res = self.client.post("/hosts/", self.org_host1)
        self.assertEqual(res.status_code, 201)
        res = self.client.post("/hosts/", self.org_host2)
        self.assertEqual(res.status_code, 201)
        res = self.client.post("/hosts/", self.sub_host1)
        self.assertEqual(res.status_code, 201)
        res = self.client.post("/hosts/", self.sub_host2)
        self.assertEqual(res.status_code, 201)
        res = self.client.post("/hosts/", self.long_host1)
        self.assertEqual(res.status_code, 201)
        res = self.client.post("/hosts/", self.long_host2)
        self.assertEqual(res.status_code, 201)

        res =  self.client.get("/hosts/{}".format(self.org_host1['name']))
        self.assertEqual(res.json()['zone'], self.zone_org.id)
        res =  self.client.get("/hosts/{}".format(self.org_host2['name']))
        self.assertEqual(res.json()['zone'], self.zone_org.id)
        res =  self.client.get("/hosts/{}".format(self.sub_host1['name']))
        self.assertEqual(res.json()['zone'], self.zone_sub.id)
        res =  self.client.get("/hosts/{}".format(self.sub_host2['name']))
        self.assertEqual(res.json()['zone'], self.zone_sub.id)
        res =  self.client.get("/hosts/{}".format(self.long_host1['name']))
        self.assertEqual(res.json()['zone'], self.zone_long.id)
        res =  self.client.get("/hosts/{}".format(self.long_host2['name']))
        self.assertEqual(res.json()['zone'], self.zone_long.id)

    def test_add_to_non_existant(self):
        data = {"name": "host1.example.net",
                "ipaddress": "10.10.0.10",
                "contact": "mail@example.org"}
        res = self.client.post("/hosts/", data)
        self.assertEqual(res.status_code, 201)
        res = self.client.get(f"/hosts/{data['name']}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['zone'], None)


    def test_rename_host_to_valid_zone(self):
        self.client.post('/hosts/', self.org_host1)
        self.client.patch('/hosts/host1.example.org',
                          {"name": "host1.example.com"})
        res = self.client.get(f"/hosts/host1.example.com")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['zone'], self.zone_com.id)

    def test_rename_host_to_unknown_zone(self):
        self.client.post('/hosts/', self.org_host1)
        self.client.patch('/hosts/host1.example.org',
                          {"name": "host1.example.net"})
        res = self.client.get(f"/hosts/host1.example.net")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['zone'], None)


class APIHostsTestCase(MregAPITestCase):
    """This class defines the test suite for api/hosts"""

    def setUp(self):
        """Define the test client and other test variables."""
        super().setUp()
        self.host_one = Host(name='host1.example.org', contact='mail1@example.org')
        self.host_two = Host(name='host2.example.org', contact='mail2@example.org')
        self.patch_data = {'name': 'new-name1.example.com', 'contact': 'updated@mail.com'}
        self.patch_data_name = {'name': 'host2.example.org', 'contact': 'updated@mail.com'}
        self.post_data = {'name': 'new-name2.example.org', "ipaddress": '127.0.0.2',
                          'contact': 'hostmaster@example.org'}
        self.post_data_name = {'name': 'host1.example.org', "ipaddress": '127.0.0.2',
                               'contact': 'hostmaster@example.org'}
        self.zone_sample = ForwardZone(name='example.org',
                                       primary_ns='ns.example.org',
                                       email='hostmaster@example.org')
        clean_and_save(self.host_one)
        clean_and_save(self.host_two)
        clean_and_save(self.zone_sample)

    def test_hosts_get_200_ok(self):
        """"Getting an existing entry should return 200"""
        response = self.client.get('/hosts/%s' % self.host_one.name)
        self.assertEqual(response.status_code, 200)

    def test_hosts_list_200_ok(self):
        """List all hosts should return 200"""
        response = self.client.get('/hosts/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['count'], 2)
        self.assertEqual(len(data['results']), 2)

    def test_hosts_get_404_not_found(self):
        """"Getting a non-existing entry should return 404"""
        response = self.client.get('/hosts/nonexistent.example.org')
        self.assertEqual(response.status_code, 404)

    def test_hosts_post_201_created(self):
        """"Posting a new host should return 201 and location"""
        response = self.client.post('/hosts/', self.post_data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response['Location'], '/hosts/%s' % self.post_data['name'])

    def test_hosts_post_400_invalid_ip(self):
        """"Posting a new host with an invalid IP should return 400"""
        post_data = {'name': 'failing.example.org', 'ipaddress': '300.400.500.600',
                     'contact': 'fail@example.org'}
        response = self.client.post('/hosts/', post_data)
        self.assertEqual(response.status_code, 400)
        response = self.client.get('/hosts/failing.example.org')
        self.assertEqual(response.status_code, 404)


    def test_hosts_post_409_conflict_name(self):
        """"Posting a new host with a name already in use should return 409"""
        response = self.client.post('/hosts/', self.post_data_name)
        self.assertEqual(response.status_code, 409)

    def test_hosts_patch_204_no_content(self):
        """Patching an existing and valid entry should return 204 and Location"""
        response = self.client.patch('/hosts/%s' % self.host_one.name, self.patch_data)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response['Location'], '/hosts/%s' % self.patch_data['name'])

    def test_hosts_patch_without_name_204_no_content(self):
        """Patching an existing entry without having name in patch should
        return 204"""
        response = self.client.patch('/hosts/%s' % self.host_one.name, {"ttl": 5000})
        self.assertEqual(response.status_code, 204)

    def test_hosts_patch_400_bad_request(self):
        """Patching with invalid data should return 400"""
        response = self.client.patch('/hosts/%s' % self.host_one.name, data={'this': 'is', 'so': 'wrong'})
        self.assertEqual(response.status_code, 400)

    def test_hosts_patch_400_bad_ttl(self):
        """Patching with invalid ttl should return 400"""
        response = self.client.patch('/hosts/%s' % self.host_one.name, data={'ttl': 100})
        self.assertEqual(response.status_code, 400)

    def test_hosts_patch_404_not_found(self):
        """Patching a non-existing entry should return 404"""
        response = self.client.patch('/hosts/feil-navn/', self.patch_data)
        self.assertEqual(response.status_code, 404)

    def test_hosts_patch_409_conflict_name(self):
        """Patching an entry with a name that already exists should return 409"""
        response = self.client.patch('/hosts/%s' % self.host_one.name, {'name': self.host_two.name})
        self.assertEqual(response.status_code, 409)


class APIMxTestcase(MregAPITestCase):
    """Test MX records."""

    def setUp(self):
        super().setUp()
        self.zone = ForwardZone(name='example.org',
                                primary_ns='ns1.example.org',
                                email='hostmaster@example.org')
        clean_and_save(self.zone)
        self.host_data = {'name': 'ns1.example.org',
                          'contact': 'mail@example.org'}
        self.client.post('/hosts/', self.host_data)
        self.host = Host.objects.get(name=self.host_data['name'])

    def test_mx_post(self):
        data = {'host': self.host.id,
                'priority': 10,
                'mx': 'smtp.example.org'}
        ret = self.client.post("/mxs/", data)
        self.assertEqual(ret.status_code, 201)

    def test_mx_post_reject_invalid(self):
        # priority is an 16 bit uint, e.g. 0..65535.
        data = {'host': self.host.id,
                'priority': -1,
                'mx': 'smtp.example.org'}
        ret = self.client.post("/mxs/", data)
        self.assertEqual(ret.status_code, 400)
        data = {'host': self.host.id,
                'priority': 1000000,
                'mx': 'smtp.example.org'}
        ret = self.client.post("/mxs/", data)
        self.assertEqual(ret.status_code, 400)
        data = {'host': self.host.id,
                'priority': 1000,
                'mx': 'invalidhostname'}
        ret = self.client.post("/mxs/", data)
        self.assertEqual(ret.status_code, 400)

    def test_mx_list(self):
        self.test_mx_post()
        ret = self.client.get("/mxs/")
        self.assertEqual(ret.status_code, 200)
        self.assertEqual(ret.data['count'], 1)

    def test_mx_delete(self):
        self.test_mx_post()
        mxs = self.client.get("/mxs/").json()['results']
        ret = self.client.delete("/mxs/{}".format(mxs[0]['id']))
        self.assertEqual(ret.status_code, 204)
        mxs = self.client.get("/mxs/").json()
        self.assertEqual(len(mxs['results']), 0)

    def test_mx_zone_autoupdate_add(self):
        self.zone.updated = False
        self.zone.save()
        self.test_mx_post()
        self.zone.refresh_from_db()
        self.assertTrue(self.zone.updated)

    def test_mx_zone_autoupdate_delete(self):
        self.test_mx_post()
        self.zone.updated = False
        self.zone.save()
        mxs = self.client.get("/mxs/").data['results']
        self.client.delete("/mxs/{}".format(mxs[0]['id']))
        self.zone.refresh_from_db()
        self.assertTrue(self.zone.updated)


class APISshfpTestcase(MregAPITestCase):
    """Test SSHFP records."""

    def setUp(self):
        super().setUp()
        self.zone = ForwardZone(name='example.org',
                                primary_ns='ns1.example.org',
                                email='hostmaster@example.org')
        clean_and_save(self.zone)
        self.host_data = {'name': 'ns1.example.org',
                          'contact': 'mail@example.org'}
        self.client.post('/hosts/', self.host_data)
        self.host = Host.objects.get(name=self.host_data['name'])

    def test_sshfp_post(self):
        data = {'host': self.host.id,
                'algorithm': 1,
                'hash_type': 1,
                'fingerprint': '0123456789abcdef'}
        ret = self.client.post("/sshfps/", data)
        self.assertEqual(ret.status_code, 201)

    def test_sshfp_post_reject_invalid(self):
        # Invalid fingerprint, algorithm, hash_type
        data = {'host': self.host.id,
                'algorithm': 1,
                'hash_type': 1,
                'fingerprint': 'beeftasty'}
        ret = self.client.post("/sshfps/", data)
        self.assertEqual(ret.status_code, 400)
        data = {'host': self.host.id,
                'algorithm': 0,
                'hash_type': 1,
                'fingerprint': '0123456789abcdef'}
        ret = self.client.post("/sshfps/", data)
        self.assertEqual(ret.status_code, 400)
        data = {'host': self.host.id,
                'algorithm': 1,
                'hash_type': 3,
                'fingerprint': '0123456789abcdef'}
        ret = self.client.post("/sshfps/", data)
        self.assertEqual(ret.status_code, 400)

    def test_sshfp_list(self):
        self.test_sshfp_post()
        ret = self.client.get("/sshfps/")
        self.assertEqual(ret.status_code, 200)
        self.assertEqual(ret.data['count'], 1)

    def test_sshfp_delete(self):
        self.test_sshfp_post()
        sshfps = self.client.get("/sshfps/").json()['results']
        ret = self.client.delete("/sshfps/{}".format(sshfps[0]['id']))
        self.assertEqual(ret.status_code, 204)
        sshfps = self.client.get("/sshfps/").json()
        self.assertEqual(len(sshfps['results']), 0)

    def test_sshfp_zone_autoupdate_add(self):
        self.zone.updated = False
        self.zone.save()
        self.test_sshfp_post()
        self.zone.refresh_from_db()
        self.assertTrue(self.zone.updated)

    def test_sshfp_zone_autoupdate_delete(self):
        self.test_sshfp_post()
        self.zone.updated = False
        self.zone.save()
        sshfps = self.client.get("/sshfps/").data['results']
        self.client.delete("/sshfps/{}".format(sshfps[0]['id']))
        self.zone.refresh_from_db()
        self.assertTrue(self.zone.updated)


class APIForwardZonesTestCase(MregAPITestCase):
    """"This class defines the test suite for forward zones API """

    def setUp(self):
        """Define the test client and other variables."""
        super().setUp()
        self.zone_one = ForwardZone(
            name="example.org",
            primary_ns="ns1.example.org",
            email="hostmaster@example.org")
        self.host_one = Host(name='ns1.example.org', contact="hostmaster@example.org")
        self.host_two = Host(name='ns2.example.org', contact="hostmaster@example.org")
        self.host_three = Host(name='ns3.example.org', contact="hostmaster@example.org")
        self.ns_one = NameServer(name='ns1.example.org', ttl=400)
        self.ns_two = NameServer(name='ns2.example.org', ttl=400)
        self.post_data_one = {'name': 'example.com',
                              'primary_ns': ['ns1.example.org', 'ns2.example.org'],
                              'email': "hostmaster@example.org",
                              'refresh': 400, 'retry': 300, 'expire': 800, 'ttl': 350}
        self.post_data_two = {'name': 'example.net',
                              'primary_ns': ['ns1.example.org', 'ns2.example.org'],
                              'email': "hostmaster@example.org"}
        self.patch_data = {'refresh': '500', 'expire': '1000'}
        clean_and_save(self.host_one)
        clean_and_save(self.host_two)
        clean_and_save(self.ns_one)
        clean_and_save(self.ns_two)
        clean_and_save(self.zone_one)

    def test_zones_get_404_not_found(self):
        """"Getting a non-existing entry should return 404"""
        response = self.client.get('/zones/nonexisting.example.org')
        self.assertEqual(response.status_code, 404)

    def test_zones_get_200_ok(self):
        """"Getting an existing entry should return 200"""
        response = self.client.get('/zones/%s' % self.zone_one.name)
        self.assertEqual(response.status_code, 200)

    def test_zones_list_200_ok(self):
        """Listing all zones should return 200"""
        response = self.client.get('/zones/')
        self.assertEqual(response.json()[0]['name'], self.zone_one.name)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.status_code, 200)

    def test_zones_post_409_name_conflict(self):
        """"Posting a entry that uses a name that is already taken should return 409"""
        response = self.client.get('/zones/%s' % self.zone_one.name)
        response = self.client.post('/zones/', {'name': response.data['name']})
        self.assertEqual(response.status_code, 409)

    def test_zones_post_201_created(self):
        """"Posting a new zone should return 201 and location"""
        response = self.client.post('/zones/', self.post_data_one)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response['Location'], '/zones/%s' % self.post_data_one['name'])

    def test_zones_post_serialno(self):
        """serialno should be based on the current date and a sequential number"""
        self.client.post('/zones/', self.post_data_one)
        self.client.post('/zones/', self.post_data_two)
        response_one = self.client.get('/zones/%s' % self.post_data_one['name'])
        response_two = self.client.get('/zones/%s' % self.post_data_two['name'])
        self.assertEqual(response_one.data['serialno'], response_two.data['serialno'])
        self.assertEqual(response_one.data['serialno'], create_serialno())

    def test_zones_patch_403_forbidden_name(self):
        """"Trying to patch the name of an entry should return 403"""
        response = self.client.get('/zones/%s' % self.zone_one.name)
        response = self.client.patch('/zones/%s' % self.zone_one.name, {'name': response.data['name']})
        self.assertEqual(response.status_code, 403)

    def test_zones_patch_403_forbidden_primary_ns(self):
        """Trying to patch the primary_ns to be a nameserver that isn't in the nameservers list should return 403"""
        response = self.client.post('/zones/', self.post_data_two)
        self.assertEqual(response.status_code, 201)
        response = self.client.patch('/zones/%s' % self.post_data_two['name'], {'primary_ns': self.host_three.name})
        self.assertEqual(response.status_code, 403)

    def test_zones_patch_404_not_found(self):
        """"Patching a non-existing entry should return 404"""
        response = self.client.patch("/zones/nonexisting.example.org", self.patch_data)
        self.assertEqual(response.status_code, 404)

    def test_zones_patch_204_no_content(self):
        """"Patching an existing entry with valid data should return 204"""
        response = self.client.patch('/zones/%s' % self.zone_one.name, self.patch_data)
        self.assertEqual(response.status_code, 204)

    def test_zones_delete_204_no_content(self):
        """"Deleting an existing entry with no conflicts should return 204"""
        response = self.client.delete('/zones/%s' % self.zone_one.name)
        self.assertEqual(response.status_code, 204)

    def test_zones_404_not_found(self):
        """"Deleting a non-existing entry should return 404"""
        response = self.client.delete("/zones/nonexisting.example.org")
        self.assertEqual(response.status_code, 404)

    def test_zones_403_forbidden(self):
        # TODO: jobb skal gjøres her
        """"Deleting an entry with registered entries should require force"""


class APIZonesForwardDelegationTestCase(MregAPITestCase):
    """ This class defines test testsuite for api/zones/<name>/delegations/
        But only for ForwardZones.
    """

    def setUp(self):
        """Define the test client and other variables."""
        super().setUp()
        self.data_exampleorg = {'name': 'example.org',
                                'primary_ns': ['ns1.example.org', 'ns2.example.org'],
                                'email': "hostmaster@example.org"}
        self.client.post("/zones/", self.data_exampleorg)

    def test_list_empty_delegation_200_ok(self):
        response = self.client.get(f"/zones/example.org/delegations/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['results'], [])

    def test_delegate_forward_201_ok(self):
        path = "/zones/example.org/delegations/"
        data = {'name': 'delegated.example.org',
                'nameservers': ['ns1.example.org', 'ns1.delegated.example.org']}
        response = self.client.post(path, data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response['Location'], f"{path}delegated.example.org")

    def test_delegate_forward_zonefiles_200_ok(self):
        self.test_delegate_forward_201_ok()
        response = self.client.get('/zonefiles/example.org')
        self.assertEqual(response.status_code, 200)

    def test_delegate_forward_badname_400_bad_request(self):
        path = "/zones/example.org/delegations/"
        bad = {'name': 'delegated.example.com',
               'nameservers': ['ns1.example.org', 'ns2.example.org']}
        response = self.client.post(path, bad)
        self.assertEqual(response.status_code, 400)

    def test_delegate_forward_no_ns_400_bad_request(self):
        path = "/zones/example.org/delegations/"
        bad = {'name': 'delegated.example.org',
               'nameservers': []}
        response = self.client.post(path, bad)
        self.assertEqual(response.status_code, 400)
        bad = {'name': 'delegated.example.org' }
        response = self.client.post(path, bad)
        self.assertEqual(response.status_code, 400)

    def test_delegate_forward_duplicate_ns_400_bad_request(self):
        path = "/zones/example.org/delegations/"
        bad = {'name': 'delegated.example.org',
               'nameservers': ['ns1.example.org', 'ns1.example.org']}
        response = self.client.post(path, bad)
        self.assertEqual(response.status_code, 400)

    def test_delegate_forward_invalid_ns_400_bad_request(self):
        path = "/zones/example.org/delegations/"
        bad = {'name': 'delegated.example.org',
               'nameservers': ['ns1', ]}
        response = self.client.post(path, bad)
        self.assertEqual(response.status_code, 400)
        bad = {'name': 'delegated.example.org',
               'nameservers': ['2"#¤2342.tld', ]}
        response = self.client.post(path, bad)
        self.assertEqual(response.status_code, 400)

    def test_delegate_forward_nameservers_list_200_ok(self):
        path = "/zones/example.org/delegations/"
        self.test_delegate_forward_201_ok()
        response = self.client.get(f"{path}delegated.example.org")
        self.assertEqual(response.status_code, 200)
        nameservers = [i['name'] for i in response.json()['nameservers']]
        self.assertEqual(len(nameservers), 2)
        for ns in nameservers:
            self.assertTrue(NameServer.objects.filter(name=ns).exists())

    def test_forward_list_delegations_200_ok(self):
        path = "/zones/example.org/delegations/"
        self.test_delegate_forward_201_ok()
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        results = response.data['results']
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['name'], 'delegated.example.org')

    def test_forward_delete_delegattion_204_ok(self):
        self.test_forward_list_delegations_200_ok()
        path = "/zones/example.org/delegations/delegated.example.org"
        self.assertEqual(NameServer.objects.count(), 3)
        response = self.client.delete(path)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response['Location'], path)
        self.assertEqual(NameServer.objects.count(), 2)
        path = "/zones/example.org/delegations/"
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['results'], [])


class APIZonesReverseDelegationTestCase(MregAPITestCase):
    """ This class defines test testsuite for api/zones/<name>/delegations/
        But only for ReverseZones.
    """

    def setUp(self):
        """Define the test client and other variables."""
        super().setUp()
        self.data_rev1010 = {'name': '10.10.in-addr.arpa',
                             'primary_ns': ['ns1.example.org', 'ns2.example.org'],
                             'email': "hostmaster@example.org"}
        self.data_revdb8 = {'name': '8.b.d.0.1.0.0.2.ip6.arpa',
                            'primary_ns': ['ns1.example.org', 'ns2.example.org'],
                            'email': "hostmaster@example.org"}

        self.del_101010 = {'name': '10.10.10.in-addr.arpa',
                           'nameservers': ['ns1.example.org', 'ns2.example.org']}
        self.del_10101010 = {'name': '10.10.10.10.in-addr.arpa',
                             'nameservers': ['ns1.example.org', 'ns2.example.org']}
        self.del_2001db810 = {'name': '0.1.0.0.8.b.d.0.1.0.0.2.ip6.arpa',
                              'nameservers': ['ns1.example.org', 'ns2.example.org']}

        self.client.post("/zones/", self.data_rev1010)
        self.client.post("/zones/", self.data_revdb8)

    def test_get_delegation_200_ok(self):
        def assertempty(data):
            response = self.client.get(f"/zones/{data['name']}/delegations/")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data['count'], 0)
            self.assertEqual(response.data['results'], [])
        for data in ('rev1010', 'revdb8'):
            assertempty(getattr(self, f"data_{data}"))

    def test_delegate_ipv4_201_ok(self):
        path = "/zones/10.10.in-addr.arpa/delegations/"
        response = self.client.post(path, self.del_101010)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response['Location'], f"{path}10.10.10.in-addr.arpa")
        response = self.client.post(path, self.del_10101010)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response['Location'], f"{path}10.10.10.10.in-addr.arpa")

    def test_delegate_ipv4_zonefiles_200_ok(self):
        self.test_delegate_ipv4_201_ok()
        response = self.client.get('/zonefiles/10.10.in-addr.arpa')
        self.assertEqual(response.status_code, 200)

    def test_delegate_ipv4_badname_400_bad_request(self):
        path = "/zones/10.10.in-addr.arpa/delegations/"
        bad = {'name': 'delegated.example.com',
               'nameservers': ['ns1.example.org', 'ns2.example.org']}
        response = self.client.post(path, bad)
        self.assertEqual(response.status_code, 400)

    def test_delegate_ipv4_invalid_zone_400_bad_request(self):
        path = "/zones/10.10.in-addr.arpa/delegations/"
        bad = {'name': '300.10.10.in-addr.arpa',
               'nameservers': ['ns1.example.org', 'ns2.example.org']}
        response = self.client.post(path, bad)
        self.assertEqual(response.status_code, 400)
        bad = {'name': '10.10.10.10.10.in-addr.arpa',
               'nameservers': ['ns1.example.org', 'ns2.example.org']}
        response = self.client.post(path, bad)
        self.assertEqual(response.status_code, 400)
        bad = {'name': 'foo.10.10.in-addr.arpa',
               'nameservers': ['ns1.example.org', 'ns2.example.org']}
        response = self.client.post(path, bad)
        self.assertEqual(response.status_code, 400)

    def test_delegate_ipv4_wrong_inet_400_bad_request(self):
        path = "/zones/10.10.in-addr.arpa/delegations/"
        bad = {'name': '0.0.0.0.0.1.0.0.8.b.d.0.1.0.0.2.ip6.arpa',
               'nameservers': ['ns1.example.org', 'ns2.example.org']}
        response = self.client.post(path, bad)
        self.assertEqual(response.status_code, 400)

    def test_delegate_duplicate_409_conflict(self):
        path = "/zones/10.10.in-addr.arpa/delegations/"
        response = self.client.post(path, self.del_101010)
        self.assertEqual(response.status_code, 201)
        response = self.client.post(path, self.del_101010)
        self.assertEqual(response.status_code, 409)

    def test_delegate_ipv6_201_ok(self):
        path = "/zones/8.b.d.0.1.0.0.2.ip6.arpa/delegations/"
        response = self.client.post(path, self.del_2001db810)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response['Location'], f"{path}{self.del_2001db810['name']}")

    def test_delegate_ipv6_zonefiles_200_ok(self):
        self.test_delegate_ipv6_201_ok()
        response = self.client.get('/zonefiles/8.b.d.0.1.0.0.2.ip6.arpa')
        self.assertEqual(response.status_code, 200)

    def test_delegate_ipv6_badname_400_bad_request(self):
        path = "/zones/8.b.d.0.1.0.0.2.ip6.arpa/delegations/"
        bad = {'name': 'delegated.example.com',
               'nameservers': ['ns1.example.org', 'ns2.example.org']}
        response = self.client.post(path, bad)
        self.assertEqual(response.status_code, 400)

    def test_delegate_ipv6_wrong_inet_400_bad_request(self):
        path = "/zones/8.b.d.0.1.0.0.2.ip6.arpa/delegations/"
        bad = {'name': '10.10.in-addr.arpa',
               'nameservers': ['ns1.example.org', 'ns2.example.org']}
        response = self.client.post(path, bad)
        self.assertEqual(response.status_code, 400)


class APIZonesNsTestCase(MregAPITestCase):
    """"This class defines the test suite for api/zones/<name>/nameservers/ """

    def setUp(self):
        """Define the test client and other variables."""
        super().setUp()
        self.post_data = {'name': 'example.org', 'primary_ns': ['ns2.example.org'],
                          'email': "hostmaster@example.org"}
        self.ns_one = Host(name='ns1.example.org', contact='mail@example.org')
        self.ns_two = Host(name='ns2.example.org', contact='mail@example.org')
        clean_and_save(self.ns_one)
        clean_and_save(self.ns_two)

    def test_zones_ns_get_200_ok(self):
        """"Getting the list of nameservers of a existing zone should return 200"""
        self.assertEqual(NameServer.objects.count(), 0)
        self.client.post('/zones/', self.post_data)
        self.assertEqual(NameServer.objects.count(), 1)
        response = self.client.get('/zones/%s/nameservers' % self.post_data['name'])
        self.assertEqual(response.status_code, 200)

    def test_zones_ns_get_404_not_found(self):
        """"Getting the list of nameservers of a non-existing zone should return 404"""
        response = self.client.delete('/zones/example.com/nameservers/')
        self.assertEqual(response.status_code, 404)

    def test_zones_ns_patch_204_no_content(self):
        """"Patching the list of nameservers with an existing nameserver should return 204"""
        self.client.post('/zones/', self.post_data)
        response = self.client.patch('/zones/%s/nameservers' % self.post_data['name'],
                                     {'primary_ns': self.post_data['primary_ns'] + [self.ns_one.name]})
        self.assertEqual(response.status_code, 204)

    def test_zones_ns_patch_400_bad_request(self):
        """"Patching the list of nameservers with a bad request body should return 404"""
        self.client.post('/zones/', self.post_data)
        response = self.client.patch('/zones/%s/nameservers' % self.post_data['name'],
                                     {'garbage': self.ns_one.name})
        self.assertEqual(response.status_code, 400)

    def test_zones_ns_patch_404_not_found(self):
        """"Patching the list of nameservers with a non-existing nameserver should return 404"""
        self.client.post('/zones/', self.post_data)
        response = self.client.patch('/zones/%s/nameservers' % self.post_data['name'],
                                     {'primary_ns': ['nonexisting-ns.example.org']})
        # XXX: This is now valid, as the NS might point to a server in a zone which we
        # don't control. Might be possible to check if the attempted NS is in a
        # zone we control and then be stricter.
        return
        self.assertEqual(response.status_code, 404)

    def test_zones_ns_delete_204_no_content_zone(self):
        """Deleting a nameserver from an existing zone should return 204"""
        self.assertFalse(NameServer.objects.exists())
        # TODO: This test needs some cleanup and work. See comments
        self.client.post('/zones/', self.post_data)

        response = self.client.patch('/zones/%s/nameservers' % self.post_data['name'],
                                     {'primary_ns': self.post_data['primary_ns'] + [self.ns_one.name]})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(NameServer.objects.count(), 2)

        response = self.client.get('/zones/%s/nameservers' % self.post_data['name'])
        self.assertEqual(response.status_code, 200)

        response = self.client.patch('/zones/%s/nameservers' % self.post_data['name'],
                                     {'primary_ns': self.ns_two.name})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(NameServer.objects.count(), 1)

        response = self.client.get('/zones/%s/nameservers' % self.post_data['name'])
        self.assertEqual(response.data, self.post_data['primary_ns'])
        response = self.client.delete('/zones/%s' % self.post_data['name'])
        self.assertEqual(response.status_code, 204)
        self.assertFalse(NameServer.objects.exists())

class APIZoneRFC2317(MregAPITestCase):
    """This class tests RFC 2317 delegations."""

    def setUp(self):
        super().setUp()
        self.data = {'name': '128/25.0.0.10.in-addr.arpa',
                     'primary_ns': ['ns1.example.org', 'ns2.example.org'],
                     'email': "hostmaster@example.org"}


    def test_create_and_get_rfc_2317_zone(self):
        # Create and get zone for 10.0.0.128/25
        response = self.client.post("/zones/", self.data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response["location"], "/zones/128/25.0.0.10.in-addr.arpa")
        response = self.client.get(response["location"])
        self.assertEqual(response.status_code, 200)


    def test_add_rfc2317_delegation_for_existing_zone(self):
        zone = {'name': '0.10.in-addr.arpa',
                'primary_ns': ['ns1.example.org', 'ns2.example.org'],
                'email': "hostmaster@example.org"}
        response = self.client.post("/zones/", zone)
        self.assertEqual(response.status_code, 201)
        delegation = {'name': '128/25.0.0.10.in-addr.arpa',
                      'nameservers': ['ns1.example.org', 'ns2.example.org']}
        response = self.client.post("/zones/0.10.in-addr.arpa/delegations/", delegation)
        self.assertEqual(response.status_code, 201)


    def test_delete_rfc2317_zone(self):
        self.client.post("/zones/", self.data)
        response = self.client.delete("/zones/128/25.0.0.10.in-addr.arpa")
        self.assertEqual(response.status_code, 204)


class APIIPaddressesTestCase(MregAPITestCase):
    """This class defines the test suite for api/ipaddresses"""

    def setUp(self):
        """Define the test client and other test variables."""
        super().setUp()
        self.host_one = Host(name='some-host.example.org',
                             contact='mail@example.org')

        self.host_two = Host(name='some-other-host.example.org',
                             contact='mail@example.com')

        clean_and_save(self.host_one)
        clean_and_save(self.host_two)

        self.ipaddress_one = Ipaddress(host=self.host_one,
                                       ipaddress='129.240.111.111')

        self.ipaddress_two = Ipaddress(host=self.host_two,
                                       ipaddress='129.240.111.112')

        clean_and_save(self.ipaddress_one)
        clean_and_save(self.ipaddress_two)

        self.post_data_full = {'host': self.host_one.id,
                               'ipaddress': '129.240.203.197'}
        self.post_data_full_conflict = {'host': self.host_one.id,
                                        'ipaddress': self.ipaddress_one.ipaddress}
        self.post_data_full_duplicate_ip = {'host': self.host_two.id,
                                            'ipaddress': self.ipaddress_one.ipaddress}
        self.patch_data_ip = {'ipaddress': '129.240.203.198'}
        self.patch_bad_ip = {'ipaddress': '129.240.300.1'}

    def test_ipaddress_get_200_ok(self):
        """"Getting an existing entry should return 200"""
        response = self.client.get('/ipaddresses/%s' % self.ipaddress_one.id)
        self.assertEqual(response.status_code, 200)

    def test_ipaddress_list_200_ok(self):
        """List all ipaddress should return 200"""
        response = self.client.get('/ipaddresses/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['count'], 2)
        self.assertEqual(len(data['results']), 2)

    def test_ipaddress_get_404_not_found(self):
        """"Getting a non-existing entry should return 404"""
        response = self.client.get('/ipaddresses/193.101.168.2')
        self.assertEqual(response.status_code, 404)

    def test_ipaddress_post_201_created(self):
        """"Posting a new ip should return 201"""
        response = self.client.post('/ipaddresses/', self.post_data_full)
        self.assertEqual(response.status_code, 201)

    def test_ipaddress_post_400_conflict_ip(self):
        """"Posting an existing ip for a host should return 400"""
        response = self.client.post('/ipaddresses/', self.post_data_full_conflict)
        self.assertEqual(response.status_code, 400)

    def test_ipaddress_post_201_two_hosts_share_ip(self):
        """"Posting a new ipaddress with an ip already in use should return 201"""
        response = self.client.post('/ipaddresses/', self.post_data_full_duplicate_ip)
        self.assertEqual(response.status_code, 201)

    def test_ipaddress_patch_200_ok(self):
        """Patching an existing and valid entry should return 200"""
        response = self.client.patch('/ipaddresses/%s' % self.ipaddress_one.id, self.patch_data_ip)
        self.assertEqual(response.status_code, 204)

    def test_ipaddress_patch_200_own_ip(self):
        """Patching an entry with its own ip should return 200"""
        response = self.client.patch('/ipaddresses/%s' % self.ipaddress_one.id,
                                     {'ipaddress': str(self.ipaddress_one.ipaddress)})
        self.assertEqual(response.status_code, 204)

    def test_ipaddress_patch_400_bad_request(self):
        """Patching with invalid data should return 400"""
        response = self.client.patch('/ipaddresses/%s' % self.ipaddress_one.id,
                                     data={'this': 'is', 'so': 'wrong'})
        self.assertEqual(response.status_code, 400)

    def test_ipaddress_patch_400_bad_ip(self):
        """Patching with invalid data should return 400"""
        response = self.client.patch('/ipaddresses/%s' % self.ipaddress_one.id, self.patch_bad_ip)
        self.assertEqual(response.status_code, 400)

    def test_ipaddress_patch_404_not_found(self):
        """Patching a non-existing entry should return 404"""
        response = self.client.patch('/ipaddresses/1234567890', self.patch_data_ip)
        self.assertEqual(response.status_code, 404)


class APIMACaddressTestCase(MregAPITestCase):
    """This class defines the test suite for api/ipaddresses with macadresses"""

    def setUp(self):
        """Define the test client and other test variables."""
        super().setUp()
        self.host_one = Host(name='host1.example.org',
                             contact='mail@example.org')

        self.host_two = Host(name='host2.example.org',
                             contact='mail@example.com')

        clean_and_save(self.host_one)
        clean_and_save(self.host_two)

        self.ipaddress_one = Ipaddress(host=self.host_one,
                                       ipaddress='10.0.0.10',
                                       macaddress='aa:bb:cc:00:00:10')

        self.ipaddress_two = Ipaddress(host=self.host_two,
                                       ipaddress='10.0.0.11',
                                       macaddress='aa:bb:cc:00:00:11')

        clean_and_save(self.ipaddress_one)
        clean_and_save(self.ipaddress_two)

        self.post_data_full = {'host': self.host_one.id,
                               'ipaddress': '10.0.0.12',
                               'macaddress': 'aa:bb:cc:00:00:12'}
        self.post_data_full_conflict = {'host': self.host_one.id,
                                        'ipaddress': self.ipaddress_one.ipaddress,
                                        'macaddress': self.ipaddress_one.macaddress}
        self.patch_mac = {'macaddress': 'aa:bb:cc:00:00:ff'}
        self.patch_mac_in_use = {'macaddress': self.ipaddress_two.macaddress}
        self.patch_ip_and_mac = {'ipaddress': '10.0.0.13',
                                 'macaddress': 'aa:bb:cc:00:00:ff'}

    def test_mac_post_ip_with_mac_201_ok(self):
        """Post a new IP with MAC should return 201 ok."""
        response = self.client.post('/ipaddresses/', self.post_data_full)
        self.assertEqual(response.status_code, 201)

    def test_mac_post_conflict_ip_and_mac_400_bad_request(self):
        """"Posting an existing IP and mac IP a host should return 400."""
        response = self.client.post('/ipaddresses/', self.post_data_full_conflict)
        self.assertEqual(response.status_code, 400)

    def test_mac_patch_mac_200_ok(self):
        """Patch an IP with a new mac should return 200 ok."""
        response = self.client.patch('/ipaddresses/%s' % self.ipaddress_one.id,
                                    self.patch_mac)
        self.assertEqual(response.status_code, 204)

    def test_mac_remove_mac_200_ok(self):
        """Patch an IP to remove MAC should return 200 ok."""
        response = self.client.patch('/ipaddresses/%s' % self.ipaddress_one.id,
                                     {'macaddress': ''})
        self.assertEqual(response.status_code, 204)

    def test_mac_patch_mac_in_use_400_bad_request(self):
        """Patch an IP with a MAC in use should return 400 bad request."""
        response = self.client.patch('/ipaddresses/%s' % self.ipaddress_one.id,
                                    self.patch_mac_in_use)
        self.assertEqual(response.status_code, 400)

    def test_mac_patch_invalid_mac_400_bad_request(self):
        """ Patch an IP with invalid MAC should return 400 bad request."""
        for mac in ('00:00:00:00:00:XX', '00:00:00:00:00', 'AA:BB:cc:dd:ee:ff'):
            response = self.client.patch('/ipaddresses/%s' % self.ipaddress_one.id,
                                         {'macaddress': mac})
            self.assertEqual(response.status_code, 400)

    def test_mac_patch_ip_and_mac_200_ok(self):
        """Patch an IP with a new IP and MAC should return 200 ok."""
        response = self.client.patch('/ipaddresses/%s' % self.ipaddress_one.id,
                                    self.patch_ip_and_mac)
        self.assertEqual(response.status_code, 204)

    def test_mac_with_network(self):
        self.network_one = Network(range='10.0.0.0/24')
        clean_and_save(self.network_one)
        self.test_mac_post_ip_with_mac_201_ok()
        self.test_mac_patch_ip_and_mac_200_ok()
        self.test_mac_patch_mac_200_ok()

    def test_mac_with_network_vlan(self):
        self.network_one = Network(range='10.0.0.0/24', vlan=10)
        self.network_two = Network(range='10.0.1.0/24', vlan=10)
        self.network_ipv6 = Network(range='2001:db8:1::/64', vlan=10)
        clean_and_save(self.network_one)
        clean_and_save(self.network_two)
        clean_and_save(self.network_ipv6)
        self.test_mac_post_ip_with_mac_201_ok()
        self.test_mac_patch_ip_and_mac_200_ok()
        self.test_mac_patch_mac_200_ok()
        # Make sure it is allowed to add a mac to both IPv4 and IPv6
        # addresses on the same vlan
        response = self.client.post('/ipaddresses/',
                                    {'host': self.host_one.id,
                                     'ipaddress': '10.0.1.10',
                                     'macaddress': '11:22:33:44:55:66'})
        self.assertEqual(response.status_code, 201)
        response = self.client.post('/ipaddresses/',
                                    {'host': self.host_one.id,
                                     'ipaddress': '2001:db8:1::10',
                                     'macaddress': '11:22:33:44:55:66'})
        self.assertEqual(response.status_code, 201)


class APICnamesTestCase(MregAPITestCase):
    """This class defines the test suite for api/cnames """
    def setUp(self):
        super().setUp()
        self.zone_one = ForwardZone(name='example.org',
                                    primary_ns='ns.example.org',
                                    email='hostmaster@example.org')
        self.zone_two = ForwardZone(name='example.net',
                                    primary_ns='ns.example.net',
                                    email='hostmaster@example.org')
        clean_and_save(self.zone_one)
        clean_and_save(self.zone_two)

        self.post_host_one = {'name': 'host1.example.org',
                              'contact': 'mail@example.org' }
        self.client.post('/hosts/', self.post_host_one)
        self.host_one = self.client.get('/hosts/%s' % self.post_host_one['name']).data
        self.post_host_two = {'name': 'host2.example.org',
                              'contact': 'mail@example.org' }
        self.client.post('/hosts/', self.post_host_two)
        self.host_two = self.client.get('/hosts/%s' % self.post_host_two['name']).data

        self.post_data = {'name': 'host-alias.example.org',
                          'host': self.host_one['id'],
                          'ttl': 5000 }

    def test_cname_post_201_ok(self):
        """ Posting a cname should return 201 OK"""
        response = self.client.post('/cnames/', self.post_data)
        self.assertEqual(response.status_code, 201)

    def test_cname_get_200_ok(self):
        """GET on an existing cname should return 200 OK."""
        self.client.post('/cnames/', self.post_data)
        response = self.client.get('/cnames/%s' % self.post_data['name'])
        self.assertEqual(response.status_code, 200)

    def test_cname_list_200_ok(self):
        """GET without name should return a list and 200 OK."""
        self.client.post('/cnames/', self.post_data)
        response = self.client.get('/cnames/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(len(response.data['results']), 1)

    def test_cname_empty_list_200_ok(self):
        """GET without name should return a list and 200 OK."""
        response = self.client.get('/cnames/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 0)
        self.assertEqual(response.data['results'], [])

    def test_cname_post_hostname_in_use_400_bad_request(self):
        response = self.client.post('/cnames/', {'host': self.host_one['id'],
                                                 'name': self.host_two['name']})
        self.assertEqual(response.status_code, 400)

    def test_cname_post_nonexistant_host_400_bad_request(self):
        """Adding a cname with a unknown host will return 400 bad request."""
        response = self.client.post('/cnames/', {'host': 1,
                                                 'name': 'alias.example.org'})
        self.assertEqual(response.status_code, 400)

    def test_cname_post_name_not_in_a_zone_400_bad_requst(self):
        """Add a cname with a name without an existing zone if forbidden"""
        response = self.client.post('/cnames/', {'host': self.host_one['id'],
                                                 'name': 'host.example.com'})
        self.assertEqual(response.status_code, 400)

    def test_cname_patch_204_ok(self):
        """ Patching a cname should return 204 OK"""
        self.client.post('/cnames/', self.post_data)
        response = self.client.patch('/cnames/%s' % self.post_data['name'],
                                     {'ttl': '500',
                                      'name': 'new-alias.example.org'})
        self.assertEqual(response.status_code, 204)


class APINetworksTestCase(MregAPITestCase):
    """"This class defines the test suite for api/networks """
    def setUp(self):
        """Define the test client and other variables."""
        super().setUp()
        self.network_sample = Network(range='10.0.0.0/24',
                                    description='some description',
                                    vlan=123,
                                    dns_delegated=False,
                                    category='so',
                                    location='Location 1',
                                    frozen=False)
        self.network_ipv6_sample = Network(range='2001:db8::/32',
                                    description='some IPv6 description',
                                    vlan=123,
                                    dns_delegated=False,
                                    category='so',
                                    location='Location 1',
                                    frozen=False)
        # Second samples are needed for the overlap tests
        self.network_sample_two = Network(range='10.0.1.0/28',
                                        description='some description',
                                        vlan=135,
                                        dns_delegated=False,
                                        category='so',
                                        location='Location 2',
                                        frozen=False)
        
        self.network_ipv6_sample_two = Network(range='2001:db8:8000::/33',
                                        description='some IPv6 description',
                                        vlan=135,
                                        dns_delegated=False,
                                        category='so',
                                        location='Location 2',
                                        frozen=False)
        
        self.host_one = Host(name='some-host.example.org',
                             contact='mail@example.org')
        clean_and_save(self.host_one)
        clean_and_save(self.network_sample)
        clean_and_save(self.network_ipv6_sample)
        clean_and_save(self.network_sample_two)
        clean_and_save(self.network_ipv6_sample_two)

        self.patch_data = {
            'description': 'Test network',
            'vlan': '435',
            'dns_delegated': 'False',
            'category': 'si',
            'location': 'new-location'
        }
        self.patch_ipv6_data = {
            'description': 'Test IPv6 network',
            'vlan': '435',
            'dns_delegated': 'False',
            'category': 'si',
            'location': 'new-location'
        }

        self.patch_data_vlan = {'vlan': '435'}
        self.patch_data_range = {'range': '10.0.0.0/28'}
        self.patch_ipv6_data_range = {'range': '2001:db8::/64'}
        self.patch_data_range_overlap = {'range': '10.0.1.0/29'}
        self.patch_ipv6_data_range_overlap = {'range': '2001:db8:8000::/34'}

        self.post_data = {
            'range': '192.0.2.0/29',
            'description': 'Test network',
            'vlan': '435',
            'dns_delegated': 'False',
        }
        self.post_ipv6_data = {
            'range': 'beef:feed::/32',
            'description': 'Test IPv6 network',
            'vlan': '435',
            'dns_delegated': 'False',
        }
        self.post_data_bad_ip = {
            'range': '192.0.2.0.95/29',
            'description': 'Test network',
            'vlan': '435',
            'dns_delegated': 'False',
        }
        self.post_ipv6_data_bad_ip = {
            'range': 'beef:good::/32',
            'description': 'Test IPv6 network',
            'vlan': '435',
            'dns_delegated': 'False',
        }
        self.post_data_bad_mask = {
            'range': '192.0.2.0/2549',
            'description': 'Test network',
            'vlan': '435',
            'dns_delegated': 'False',
        }
        self.post_ipv6_data_bad_mask = {
            'range': 'beef:feed::/129',
            'description': 'Test IPv6 network',
            'vlan': '435',
            'dns_delegated': 'False',
        }
        self.post_data_overlap = {
            'range': '10.0.1.0/29',
            'description': 'Test network',
            'vlan': '435',
            'dns_delegated': 'False',
        }
        self.post_ipv6_data_overlap = {
            'range': '2001:db8:8000::/34',
            'description': 'Test IPv6 network',
            'vlan': '435',
            'dns_delegated': 'False',
        }

    def test_networks_post_201_created(self):
        """Posting a network should return 201"""
        response = self.client.post('/networks/', self.post_data)
        self.assertEqual(response.status_code, 201)

    def test_ipv6_networks_post_201_created(self):
        """Posting an IPv6 network should return 201"""
        response = self.client.post('/networks/', self.post_ipv6_data)
        self.assertEqual(response.status_code, 201)
    
    def test_networks_post_400_bad_request_ip(self):
        """Posting a network with a range that has a malformed IP should return 400"""
        response = self.client.post('/networks/', self.post_data_bad_ip)
        self.assertEqual(response.status_code, 400)
    
    def test_ipv6_networks_post_400_bad_request_ip(self):
        """Posting an IPv6 network with a range that has a malformed IP should return 400"""
        response = self.client.post('/networks/', self.post_ipv6_data_bad_ip)
        self.assertEqual(response.status_code, 400)

    def test_networks_post_400_bad_request_mask(self):
        """Posting a network with a range that has a malformed mask should return 400"""
        response = self.client.post('/networks/', self.post_data_bad_mask)
        self.assertEqual(response.status_code, 400)

    def test_ipv6_networks_post_400_bad_request_mask(self):
        """Posting an IPv6 network with a range that has a malformed mask should return 400"""
        response = self.client.post('/networks/', self.post_ipv6_data_bad_mask)
        self.assertEqual(response.status_code, 400)

    def test_networks_post_409_overlap_conflict(self):
        """Posting a network with a range which overlaps existing should return 409"""
        response = self.client.post('/networks/', self.post_data_overlap)
        self.assertEqual(response.status_code, 409)

    def test_ipv6_networks_post_409_overlap_conflict(self):
        """Posting an IPv6 network with a range which overlaps existing should return 409"""
        response = self.client.post('/networks/', self.post_ipv6_data_overlap)
        self.assertEqual(response.status_code, 409)

    def test_networks_get_200_ok(self):
        """GET on an existing ip-range should return 200 OK."""
        response = self.client.get('/networks/%s' % self.network_sample.range)
        self.assertEqual(response.status_code, 200)

    def test_ipv6_networks_get_200_ok(self):
        """GET on an existing ipv6-range should return 200 OK."""
        response = self.client.get('/networks/%s' % self.network_ipv6_sample.range)
        self.assertEqual(response.status_code, 200)

    def test_networks_list_200_ok(self):
        """GET without name should return a list and 200 OK."""
        response = self.client.get('/networks/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 4)
        self.assertEqual(len(response.data['results']), 4)

    def test_networks_patch_204_no_content(self):
        """Patching an existing and valid entry should return 204 and Location"""
        response = self.client.patch('/networks/%s' % self.network_sample.range, self.patch_data)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response['Location'], '/networks/%s' % self.network_sample.range)

    def test_ipv6_networks_patch_204_no_content(self):
        """Patching an existing and valid IPv6 entry should return 204 and Location"""
        response = self.client.patch('/networks/%s' % self.network_ipv6_sample.range, self.patch_ipv6_data)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response['Location'], '/networks/%s' % self.network_ipv6_sample.range)

    def test_networks_patch_204_non_overlapping_range(self):
        """Patching an entry with a non-overlapping range should return 204"""
        response = self.client.patch('/networks/%s' % self.network_sample.range, data=self.patch_data_range)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response['Location'], '/networks/%s' % self.patch_data_range['range'])

    def test_ipv6_networks_patch_204_non_overlapping_range(self):
        """Patching an entry with a non-overlapping IPv6 range should return 204"""
        response = self.client.patch('/networks/%s' % self.network_ipv6_sample.range, data=self.patch_ipv6_data_range)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response['Location'], '/networks/%s' % self.patch_ipv6_data_range['range'])

    def test_networks_patch_400_bad_request(self):
        """Patching with invalid data should return 400"""
        response = self.client.patch('/networks/%s' % self.network_sample.range,
                                     data={'this': 'is', 'so': 'wrong'})
        self.assertEqual(response.status_code, 400)

    def test_ipv6_networks_patch_400_bad_request(self):
        """Patching with invalid IPv6 data should return 400"""
        response = self.client.patch('/networks/%s' % self.network_ipv6_sample.range,
                                     data={'this': 'is', 'so': 'wrong'})
        self.assertEqual(response.status_code, 400)

    def test_networks_patch_404_not_found(self):
        """Patching a non-existing entry should return 404"""
        response = self.client.patch('/networks/193.101.168.0/29', self.patch_data)
        self.assertEqual(response.status_code, 404)

    def test_ipv6_networks_patch_404_not_found(self):
        """Patching a non-existing IPv6 entry should return 404"""
        response = self.client.patch('/networks/3000:4000:5000:6000::/64', self.patch_ipv6_data)
        self.assertEqual(response.status_code, 404)

    def test_networks_patch_409_forbidden_range(self):
        """Patching an entry with an overlapping range should return 409"""
        response = self.client.patch('/networks/%s' % self.network_sample.range,
                data=self.patch_data_range_overlap)
        self.assertEqual(response.status_code, 409)

    def test_ipv6_networks_patch_409_forbidden_range(self):
        """Patching an IPv6 entry with an overlapping range should return 409"""
        response = self.client.patch('/networks/%s' % self.network_ipv6_sample.range,
                data=self.patch_ipv6_data_range_overlap)
        self.assertEqual(response.status_code, 409)

    def test_networks_get_network_by_ip_200_ok(self):
        """GET on an ip in a known network should return 200 OK."""
        response = self.client.get('/networks/ip/10.0.0.5')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['range'], self.network_sample.range)

    def test_ipv6_networks_get_network_by_ip_200_ok(self):
        """GET on an IPv6 in a known network should return 200 OK."""
        response = self.client.get('/networks/ip/2001:db8::12')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['range'], self.network_ipv6_sample.range)

    def test_networks_get_network_unknown_by_ip_404_not_found(self):
        """GET on an IP in a unknown network should return 404 not found."""
        response = self.client.get('/networks/ip/127.0.0.1')
        self.assertEqual(response.status_code, 404)

    def test_ipv6_networks_get_network_unknown_by_ip_404_not_found(self):
        """GET on an IPv6 in a unknown network should return 404 not found."""
        response = self.client.get('/networks/ip/7000:8000:9000:a000::feed')
        self.assertEqual(response.status_code, 404)

    def test_networks_get_usedcount_200_ok(self):
        """GET on /networks/<ip/mask>/used_count return 200 ok and data."""
        ip_sample = Ipaddress(host=self.host_one, ipaddress='10.0.0.17')
        clean_and_save(ip_sample)

        response = self.client.get('/networks/%s/used_count' % self.network_sample.range)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, 1)

    def test_ipv6_networks_get_usedcount_200_ok(self):
        """GET on /networks/<ipv6/mask>/used_count return 200 ok and data."""
        ipv6_sample = Ipaddress(host=self.host_one, ipaddress='2001:db8::beef')
        clean_and_save(ipv6_sample)

        response = self.client.get('/networks/%s/used_count' % self.network_ipv6_sample.range)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, 1)

    def test_networks_get_usedlist_200_ok(self):
        """GET on /networks/<ip/mask>/used_list should return 200 ok and data."""
        ip_sample = Ipaddress(host=self.host_one, ipaddress='10.0.0.17')
        clean_and_save(ip_sample)

        response = self.client.get('/networks/%s/used_list' % self.network_sample.range)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, ['10.0.0.17'])

    def test_ipv6_networks_get_usedlist_200_ok(self):
        """GET on /networks/<ipv6/mask>/used_list should return 200 ok and data."""
        ipv6_sample = Ipaddress(host=self.host_one, ipaddress='2001:db8::beef')
        clean_and_save(ipv6_sample)

        response = self.client.get('/networks/%s/used_list' % self.network_ipv6_sample.range)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, ['2001:db8::beef'])

    def test_networks_get_unusedcount_200_ok(self):
        """GET on /networks/<ip/mask>/unused_count should return 200 ok and data."""
        ip_sample = Ipaddress(host=self.host_one, ipaddress='10.0.0.17')
        clean_and_save(ip_sample)

        response = self.client.get('/networks/%s/unused_count' % self.network_sample.range)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, 250)

    def test_ipv6_networks_get_unusedcount_200_ok(self):
        """GET on /networks/<ipv6/mask>/unused_count should return 200 ok and data."""
        ipv6_sample = Ipaddress(host=self.host_one, ipaddress='2001:db8::beef')
        clean_and_save(ipv6_sample)

        response = self.client.get('/networks/%s/unused_count' % self.network_ipv6_sample.range)
        self.assertEqual(response.status_code, 200)
        # Only the first 4000 addresses for IPv6 are returned, :1 and :2 and :3 are reserved
        self.assertEqual(response.data, 3997)

    def test_networks_get_unusedlist_200_ok(self):
        """GET on /networks/<ip/mask>/unused_list should return 200 ok and data."""
        ip_sample = Ipaddress(host=self.host_one, ipaddress='10.0.0.17')
        clean_and_save(ip_sample)

        response = self.client.get('/networks/%s/unused_list' % self.network_sample.range)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 250)

    def test_ipv6_networks_get_unusedlist_200_ok(self):
        """GET on /networks/<ipv6/mask>/unused_list should return 200 ok and data."""
        ipv6_sample = Ipaddress(host=self.host_one, ipaddress='2001:db8::beef')
        clean_and_save(ipv6_sample)

        response = self.client.get('/networks/%s/unused_list' % self.network_ipv6_sample.range)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 3997)

    def test_networks_get_first_unused_200_ok(self):
        """GET on /networks/<ip/mask>/first_unused should return 200 ok and data."""
        ip_sample = Ipaddress(host=self.host_one, ipaddress='10.0.0.17')
        clean_and_save(ip_sample)

        response = self.client.get('/networks/%s/first_unused' % self.network_sample.range)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, '10.0.0.4')

    def test_ipv6_networks_get_first_unused_200_ok(self):
        """GET on /networks/<ipv6/mask>/first_unused should return 200 ok and data."""
        ipv6_sample = Ipaddress(host=self.host_one, ipaddress='2001:db8::beef')
        clean_and_save(ipv6_sample)

        response = self.client.get('/networks/%s/first_unused' % self.network_ipv6_sample.range)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, '2001:db8::4')

    def test_networks_get_ptroverride_list(self):
        """GET on /networks/<ip/mask>/ptroverride_list should return 200 ok and data."""
        response = self.client.get('/networks/%s/ptroverride_list' % self.network_sample.range)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])
        ptr = PtrOverride(host=self.host_one, ipaddress='10.0.0.10')
        clean_and_save(ptr)
        response = self.client.get('/networks/%s/ptroverride_list' % self.network_sample.range)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, ['10.0.0.10'])

    def test_ipv6_networks_get_ptroverride_list(self):
        """GET on /networks/<ipv6/mask>/ptroverride_list should return 200 ok and data."""
        response = self.client.get('/networks/%s/ptroverride_list' % self.network_ipv6_sample.range)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])
        ptr = PtrOverride(host=self.host_one, ipaddress='2001:db8::feed')
        clean_and_save(ptr)
        response = self.client.get('/networks/%s/ptroverride_list' % self.network_ipv6_sample.range)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, ['2001:db8::feed'])

    def test_networks_get_reserved_list(self):
        """GET on /networks/<ip/mask>/reserverd_list should return 200 ok and data."""
        response = self.client.get('/networks/%s/reserved_list' % self.network_sample.range)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, ['10.0.0.0', '10.0.0.1',
            '10.0.0.2', '10.0.0.3','10.0.0.255'])

    def test_ipv6_networks_get_reserved_list(self):
        """GET on /networks/<ipv6/mask>/reserverd_list should return 200 ok and data."""
        response = self.client.get('/networks/%s/reserved_list' % self.network_ipv6_sample.range)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, ['2001:db8::', '2001:db8::1',
            '2001:db8::2', '2001:db8::3'])

    def test_networks_delete_204_no_content(self):
        """Deleting an existing entry with no adresses in use should return 204"""
        response = self.client.post('/networks/', self.post_data)
        self.assertEqual(response.status_code, 201)
        response = self.client.delete('/networks/%s' % self.post_data['range'])
        self.assertEqual(response.status_code, 204)

    def test_ipv6_networks_delete_204_no_content(self):
        """Deleting an existing IPv6 entry with no adresses in use should return 204"""
        response = self.client.post('/networks/', self.post_ipv6_data)
        self.assertEqual(response.status_code, 201)
        response = self.client.delete('/networks/%s' % self.post_ipv6_data['range'])
        self.assertEqual(response.status_code, 204)

    def test_networks_delete_409_conflict(self):
        """Deleting an existing entry with  adresses in use should return 409"""
        response = self.client.post('/networks/', self.post_data)
        self.assertEqual(response.status_code, 201)

        ip_sample = Ipaddress(host=self.host_one, ipaddress='192.0.2.1')
        clean_and_save(ip_sample)

        response = self.client.delete('/networks/%s' % self.post_data['range'])
        self.assertEqual(response.status_code, 409)

    def test_ipv6_networks_delete_409_conflict(self):
        """Deleting an existing IPv6 entry with adresses in use should return 409"""
        response = self.client.post('/networks/', self.post_ipv6_data)
        self.assertEqual(response.status_code, 201)

        ipv6_sample = Ipaddress(host=self.host_one, ipaddress='beef:feed::beef')
        clean_and_save(ipv6_sample)

        response = self.client.delete('/networks/%s' % self.post_ipv6_data['range'])
        self.assertEqual(response.status_code, 409)


class APIModelChangeLogsTestCase(MregAPITestCase):
    """This class defines the test suite for api/history """

    def setUp(self):
        """Define the test client and other variables."""
        super().setUp()
        self.host_one = Host(name='some-host.example.org',
                             contact='mail@example.org',
                             ttl=300,
                             loc='23 58 23 N 10 43 50 E 80m',
                             comment='some comment')
        clean_and_save(self.host_one)

        self.log_data = {'host': self.host_one.id,
                         'name': self.host_one.name,
                         'contact': self.host_one.contact,
                         'ttl': self.host_one.ttl,
                         'loc': self.host_one.loc,
                         'comment': self.host_one.comment}

        self.log_entry_one = ModelChangeLog(table_name='hosts',
                                            table_row=self.host_one.id,
                                            data=self.log_data,
                                            action='saved',
                                            timestamp=timezone.now())
        clean_and_save(self.log_entry_one)

    def test_history_get_200_OK(self):
        """Get on /history/ should return a list of table names that have entries, and 200 OK."""
        response = self.client.get('/history/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('hosts', response.data)

    def test_history_host_get_200_OK(self):
        """Get on /history/hosts/<pk> should return a list of dicts containing entries for that host"""
        response = self.client.get('/history/hosts/{}'.format(self.host_one.id))
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.data, list)
