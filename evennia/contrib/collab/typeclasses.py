from evennia import CmdSet
from evennia.commands import cmdset
from evennia.contrib.collab.perms import collab_check, get_owner, set_owner
from evennia.contrib.collab.template.core import evtemplate
from evennia.objects.objects import DefaultObject, DefaultRoom, DefaultExit, DefaultCharacter
from evennia.players.players import DefaultPlayer
from evennia.typeclasses.models import AttributeHandler, DbHolder

# Separate namespaces for different attributes. Each of these classes'
# docstrings explains their intended scope and permissions level. However,
# the appropriate locks can be overwritten in the settings file. See
# collab_settings.py
from evennia.utils import lazy_property, make_iter
from evennia.utils.search import search_object_by_tag


class WizHiddenAttributeHandler(AttributeHandler):
    """
    Attributes which are for wizards to read and write to, but which players
    should not be able to see.

    Examples include
    """
    _attrtype = 'wizh'


class WizAttributeHandler(AttributeHandler):
    """
    Attributes which are accessible to wizards but which the player can
    see.

    Examples include quota overrides, values that are user-settable via
    command but cannot be allowed to be set via the @set command, etc.
    """
    _attrtype = 'wiz'


class ImmortalHiddenAttributeHandler(AttributeHandler):
    """
    Attributes which are more for internal accounting and/or security and
    which should not be exposed to anyone less powerful than an immortal.

    Examples include things like IP information, etc.

    In most cases, the standard 'db' field will work fine for this, since
    collab avoids touching it. However, this type provides a semantic
    distinction, and prevents this data from being exposed should a command
    that isn't collab-aware provide access to information on 'db'.
    """
    _attrtype = 'imh'


class PublicAttributeHandler(AttributeHandler):
    """
    Handles attributes that are public for anyone to set or read.

    These are inherently untrustworthy. They should primarily be used for
    inconsequential storage to spruce up a room without requiring additional
    modules. For example, a description might differ if someone has been to a
    room before. The room could set an attribute on the user to check for this.
    """
    _attrtype = 'pub'


class UserAttributeHandler(AttributeHandler):
    """
    Handles Attributes that are intended for the user to set on themselves.

    These are publicly readable. Examples might include: description, species,
    and sex
    """
    _attrtype = 'usr'


class UserHiddenAttributeHandler(AttributeHandler):
    """
    Handles attributes that the user can set on themselves but which don't need
    to be publicly readable, such as a user's inactive saved descriptions.
    """
    _attrtype = 'usrh'


initializers = [WizHiddenAttributeHandler, WizAttributeHandler,
                ImmortalHiddenAttributeHandler, PublicAttributeHandler,
                UserAttributeHandler, UserHiddenAttributeHandler]


class CollabBase(object):

    template_permitted = ('name',)

    def check_protected(self):
        """
        Objects may be marked as 'protected', preventing them from being
        fiddled with by people who normally pass the bypass lock.
        """
        return self.imhdb.protected

    @property
    def owner(self):
        return get_owner(self)

    @owner.setter
    def owner(self, value):
        set_owner(value, self)

    def return_appearance(self, looker):
        """
        This formats a description. It is the hook a 'look' command
        should call.

        Args:
            looker (Object): Object doing the looking.
        """
        if not looker:
            return
        # get and identify all objects
        visible = (con for con in self.contents if con != looker and
                   con.access(looker, "view"))
        exits, users, things = [], [], []
        for con in visible:
            key = con.get_display_name(looker)
            if con.destination:
                exits.append(key)
            elif con.has_player:
                users.append("{c%s{n" % key)
            else:
                things.append(key)
        # get description, build string
        string = "{c%s{n\n" % self.get_display_name(looker)
        desc = evtemplate(self.db.desc or '', run_as=self.owner, me=looker, this=self, how='desc')
        if desc:
            string += "%s" % desc
        if exits:
            string += "\n{wExits:{n " + ", ".join(exits)
        if users or things:
            string += "\n{wYou see:{n " + ", ".join(users + things)
        return string


for init in initializers:
    key = init._attrtype
    name = init._attrtype + "attributes"
    label = key + 'db'

    # Need to be mindful of closure scoping. Names are looked up at runtime, so
    # we need a function here to freeze the names for these functions.
    # Note that we're doing some variable name shadowing here, taking advantage of
    # variable scoping.
    def make_descriptors(init, key, name, label):
        def getter(self):
            try:
                return getattr(self, '_%s_holder' % key)
            except AttributeError:
                setattr(self, '_%s_holder' % key, DbHolder(self, name, manager_name=key + 'attributes'))
                return getattr(self, '_%s_holder' % key)

        def setter(self):
            string = "Cannot assign directly to %s object! " % label
            string += "Use %s.attr=value instead." % label
            raise Exception(string)

        def deleter(self):
            "Stop accidental deletion."
            raise Exception("Cannot delete the %s object!" % label)

        def attributes(self):
            return init(self)

        # Needed for the lazy loader.
        attributes.__name__ = name

        return getter, setter, deleter, attributes

    getter, setter, deleter, attributes = make_descriptors(init, key, name, label)

    setattr(CollabBase, label, property(getter, setter, deleter))
    setattr(CollabBase, name, lazy_property(attributes))


class CollabObject(CollabBase, DefaultObject):
    pass


class CollabCharacter(CollabBase, DefaultCharacter):
    pass


class CollabExit(CollabBase, DefaultExit):
    priority = -30

    def create_exit_cmdset(self, exidbobj, location=None):
        """
        Helper function for creating an exit command set + command.

        The command of this cmdset has the same name as the Exit
        object and allows the exit to react when the player enter the
        exit's name, triggering the movement between rooms.

        Unlike the normal exit commandset, this one checks to make sure that it
        has permission to create an exit within the container it resides.

        Args:
            exidbobj (Object): The DefaultExit object to base the command on.

        """
        # If something is unowned, that means it was probably created by a lib or pre-collab.
        location = location or self.location
        if self.owner:
            if not collab_check(self.owner, location, locks=['open_exit']):
                return

        # create an exit command. We give the properties here,
        # to always trigger metaclass preparations
        cmd = self.exit_command(key=exidbobj.db_key.strip().lower(),
                                aliases=exidbobj.aliases.all(),
                                locks=str(exidbobj.locks),
                                auto_help=False,
                                destination=exidbobj.db_destination,
                                arg_regex=r"^$",
                                is_exit=True,
                                obj=exidbobj)
        # create a cmdset
        exit_cmdset = cmdset.CmdSet(None)
        exit_cmdset.key = 'ExitCmdSet'
        if self.wizdb.exit_priority is not None:
            priority = self.priority
        else:
            priority = self.wizdb.exit_priority
        exit_cmdset.priority = priority
        exit_cmdset.duplicates = True
        # add command to cmdset
        exit_cmdset.add(cmd)
        return exit_cmdset


class CollabRoom(CollabBase, DefaultRoom):

    def zone_exit_commandset(self):
        """
        Gets a commandset of all exits tagged for an 'area'.
        """
        exit_tags = self.tags.get(category='zone_room')
        if exit_tags:
            exit_tags = make_iter(exit_tags)
        else:
            exit_tags = []
        exits = []
        for tag in exit_tags:
            exits.extend(search_object_by_tag(category='zone_exit', key=tag))
        cmdset = CmdSet(self)
        for exit in exits:
            if exit.destination and hasattr(exit, 'create_exit_cmdset'):
                cmdset.add(exit.create_exit_cmdset(exit, location=self))
        return cmdset

    def reset_zone_cmdset(self):
        if self.ndb.zone_cmdset:
            self.cmdset.remove(self.ndb.zone_cmdset)
            del self.ndb.zone_cmdset

    def at_cmdset_get(self, **kwargs):
        if not self.ndb.zone_cmdset:
            self.ndb.zone_cmdset = self.zone_exit_commandset()
            self.cmdset.add(self.ndb.zone_cmdset)


class CollabPlayer(CollabBase, DefaultPlayer):
    pass
