from __future__ import annotations
import discord
from discord import app_commands
from discord.ui import View, Select, Button, Modal, TextInput
import requests

TOKEN = "MTE4OTcwNDQxNTI5MzIxODg1Nw.GMBkUH.K9iPKCUE65sKfvQGHYJumONk17NBOtzzJv8H6E"
API_BASE = "http://127.0.0.1:8000"
API_KEY = "CHANGE_ME_TO_A_LONG_SECRET"

FORUM_ID = 1458957861903138836
STAFF_ROLE_ID = 1189718743937458286

HEADERS = {"X-API-Key": API_KEY}
TIMEOUT = 10

CASE_TYPES = ["R-Individual", "R-Discord", "R-Group", "D-Server", "ROBLOX", "Discord"]
PLATFORMS = ["Discord", "ROBLOX", "External"]
INTEL_TYPES = ["ALT", "NOTE", "FLAG"]


# ------------------------
# API helpers
# ------------------------

def api_post(path: str, payload: dict):
    return requests.post(API_BASE + path, json=payload, headers=HEADERS, timeout=TIMEOUT)

def api_patch(path: str, payload: dict):
    return requests.patch(API_BASE + path, json=payload, headers=HEADERS, timeout=TIMEOUT)

def api_get(path: str):
    return requests.get(API_BASE + path, headers=HEADERS, timeout=TIMEOUT)

def must_json(resp: requests.Response) -> dict:
    if resp.status_code != 200:
        raise RuntimeError(f"API {resp.status_code}: {resp.text[:200]}")
    return resp.json()


# ------------------------
# Staff lock
# ------------------------

def is_staff_member(member: discord.Member) -> bool:
    # Admin override
    if member.guild_permissions.administrator:
        return True
    return any(r.id == STAFF_ROLE_ID for r in member.roles)

def staff_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        return is_staff_member(interaction.user)
    return app_commands.check(predicate)


# ------------------------
# Thread posting with lock behavior
# ------------------------

async def get_thread(guild: discord.Guild, thread_id: int) -> discord.Thread:
    t = guild.get_thread(thread_id)
    if t:
        return t
    ch = await guild.fetch_channel(thread_id)
    if isinstance(ch, discord.Thread):
        return ch
    raise RuntimeError("Thread not found")

async def post_to_thread_locked(thread: discord.Thread, content: str):
    """
    Threads are locked for integrity. To append updates, the bot:
    - temporarily unlocks
    - posts
    - relocks
    Requires Manage Threads permission.
    """
    # Try direct post first (some configs allow it)
    try:
        await thread.send(content)
        return
    except discord.Forbidden:
        pass

    # Unlock -> post -> relock
    await thread.edit(locked=False)
    try:
        await thread.send(content)
    finally:
        await thread.edit(locked=True)


# ------------------------
# Bot core
# ------------------------

class RegistryBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

bot = RegistryBot()


# ------------------------
# Modal for reason
# ------------------------

class ReasonModal(Modal, title="Case Reason"):
    reason = TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=1600)

    def __init__(self, identifier: str, case_type: str, platform: str):
        super().__init__()
        self.identifier = identifier
        self.case_type = case_type
        self.platform = platform

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # 1) Create case in registry
        resp = api_post("/api/cases", {
            "identifier": self.identifier,
            "platform": self.platform,
            "case_type": self.case_type,
            "reason": self.reason.value,
            "author": f"{interaction.user} ({interaction.user.id})"
        })
        data = must_json(resp)
        case_id = data["case_id"]
        user_id = data["user_id"]

        # 2) Create forum thread
        forum = interaction.guild.get_channel(FORUM_ID)
        if forum is None:
            raise RuntimeError("Forum channel not found. Check FORUM_ID.")
        if not isinstance(forum, discord.ForumChannel):
            raise RuntimeError("FORUM_ID must be a ForumChannel.")

        created = await forum.create_thread(
            name=f"Case #{case_id} | {self.identifier}",
            content=(
                f"**Averin Holdings | NDS Registry**\n"
                f"**Case Created**\n\n"
                f"**Case:** `#{case_id}`\n"
                f"**User:** `{self.identifier}` (User ID: `{user_id}`)\n"
                f"**Case Type:** `{self.case_type}`\n"
                f"**Platform:** `{self.platform}`\n"
                f"**Reason:**\n```{self.reason.value}```\n"
                f"**Audit:** Thread is locked for integrity. Updates are appended by staff via bot commands."
            )
        )

        # discord.py can return Thread or ThreadWithMessage depending on version
        thread = getattr(created, "thread", created)

        # 3) Lock thread immediately
        await thread.edit(locked=True)

        # 4) Store thread_id in registry + event log
        must_json(api_patch(f"/api/cases/{case_id}", {
            "thread_id": str(thread.id),
            "log_message": "Forum thread created + locked",
            "author": f"{interaction.user} ({interaction.user.id})"
        }))

        # 5) Confirm
        await interaction.followup.send(
            f"✅ Case **#{case_id}** created.\n"
            f"Thread: {thread.mention}\n"
            f"Web: {API_BASE}/case/{case_id}",
            ephemeral=True
        )


# ------------------------
# Interactive create view
# ------------------------

class CaseCreateView(View):
    def __init__(self, identifier: str):
        super().__init__(timeout=300)
        self.identifier = identifier
        self.case_type: str | None = None
        self.platform: str | None = None

        self.type_select = Select(
            placeholder="Select case type…",
            options=[discord.SelectOption(label=t) for t in CASE_TYPES],
            min_values=1,
            max_values=1
        )
        self.type_select.callback = self.on_type_selected
        self.add_item(self.type_select)

        self.platform_select = Select(
            placeholder="Select platform…",
            options=[discord.SelectOption(label=p) for p in PLATFORMS],
            min_values=1,
            max_values=1
        )
        self.platform_select.callback = self.on_platform_selected
        self.add_item(self.platform_select)

        self.reason_btn = Button(label="Open Reason Form", style=discord.ButtonStyle.primary)
        self.reason_btn.callback = self.on_reason_clicked
        self.add_item(self.reason_btn)

    async def on_type_selected(self, interaction: discord.Interaction):
        self.case_type = interaction.data["values"][0]
        await interaction.response.defer(ephemeral=True)

    async def on_platform_selected(self, interaction: discord.Interaction):
        self.platform = interaction.data["values"][0]
        await interaction.response.defer(ephemeral=True)

    async def on_reason_clicked(self, interaction: discord.Interaction):
        if not self.case_type or not self.platform:
            await interaction.response.send_message(
                "Please select **case type** and **platform** first.",
                ephemeral=True
            )
            return
        await interaction.response.send_modal(ReasonModal(self.identifier, self.case_type, self.platform))


# ------------------------
# Commands
# ------------------------

@bot.tree.command(name="case-create", description="Create a case using the guided form")
@staff_check()
async def case_create(interaction: discord.Interaction, username: str):
    await interaction.response.send_message(
        "Please complete the case setup:",
        view=CaseCreateView(username),
        ephemeral=True
    )


@bot.tree.command(name="case-update", description="Append an operational update to a case (posts to thread)")
@staff_check()
async def case_update(interaction: discord.Interaction, caseid: int, update: str):
    await interaction.response.defer(ephemeral=True)

    # Log in registry
    must_json(api_post(f"/api/cases/{caseid}/events", {
        "event_type": "NOTE",
        "message": update,
        "author": f"{interaction.user} ({interaction.user.id})"
    }))

    # Post to thread
    case = must_json(api_get(f"/api/cases/{caseid}"))["case"]
    thread_id = case.get("thread_id")
    if not thread_id:
        await interaction.followup.send("Case has no thread_id linked yet.", ephemeral=True)
        return

    thread = await get_thread(interaction.guild, int(thread_id))
    await post_to_thread_locked(thread, f"**Case Update** by {interaction.user}:\n```{update}```")

    await interaction.followup.send("✅ Update logged + posted to thread.", ephemeral=True)


@bot.tree.command(name="edit-case", description="Edit the case reason (posts update to thread)")
@staff_check()
async def edit_case(interaction: discord.Interaction, caseid: int, new_reason: str):
    await interaction.response.defer(ephemeral=True)

    must_json(api_patch(f"/api/cases/{caseid}", {
        "reason": new_reason,
        "log_message": "Reason updated",
        "author": f"{interaction.user} ({interaction.user.id})"
    }))

    case = must_json(api_get(f"/api/cases/{caseid}"))["case"]
    if case.get("thread_id"):
        thread = await get_thread(interaction.guild, int(case["thread_id"]))
        await post_to_thread_locked(thread, f"**Reason Updated** by {interaction.user}:\n```{new_reason}```")

    await interaction.followup.send("✅ Reason updated.", ephemeral=True)


@bot.tree.command(name="case-close", description="Close a case (posts to thread)")
@staff_check()
async def case_close(interaction: discord.Interaction, caseid: int):
    await interaction.response.defer(ephemeral=True)

    must_json(api_patch(f"/api/cases/{caseid}", {
        "status": "CLOSED",
        "log_message": "Status changed to CLOSED",
        "author": f"{interaction.user} ({interaction.user.id})"
    }))

    case = must_json(api_get(f"/api/cases/{caseid}"))["case"]
    if case.get("thread_id"):
        thread = await get_thread(interaction.guild, int(case["thread_id"]))
        await post_to_thread_locked(thread, f"🔒 **Case CLOSED** by {interaction.user}.")

    await interaction.followup.send("✅ Case closed.", ephemeral=True)


@bot.tree.command(name="case-reopen", description="Reopen a closed case (posts to thread)")
@staff_check()
async def case_reopen(interaction: discord.Interaction, caseid: int):
    await interaction.response.defer(ephemeral=True)

    must_json(api_patch(f"/api/cases/{caseid}", {
        "status": "OPEN",
        "log_message": "Status changed to OPEN",
        "author": f"{interaction.user} ({interaction.user.id})"
    }))

    case = must_json(api_get(f"/api/cases/{caseid}"))["case"]
    if case.get("thread_id"):
        thread = await get_thread(interaction.guild, int(case["thread_id"]))
        await post_to_thread_locked(thread, f"🔓 **Case REOPENED** by {interaction.user}.")

    await interaction.followup.send("✅ Case reopened.", ephemeral=True)


@bot.tree.command(name="case-archive", description="Archive a case (posts to thread)")
@staff_check()
async def case_archive(interaction: discord.Interaction, caseid: int):
    await interaction.response.defer(ephemeral=True)

    must_json(api_patch(f"/api/cases/{caseid}", {
        "status": "ARCHIVED",
        "log_message": "Status changed to ARCHIVED",
        "author": f"{interaction.user} ({interaction.user.id})"
    }))

    case = must_json(api_get(f"/api/cases/{caseid}"))["case"]
    if case.get("thread_id"):
        thread = await get_thread(interaction.guild, int(case["thread_id"]))
        await post_to_thread_locked(thread, f"📦 **Case ARCHIVED** by {interaction.user}.")

    await interaction.followup.send("✅ Case archived.", ephemeral=True)


@bot.tree.command(name="user-intel", description="Add staff intel to a user (also posts to their latest case thread)")
@staff_check()
async def user_intel(interaction: discord.Interaction, identifier: str, intel_type: str, value: str):
    await interaction.response.defer(ephemeral=True)

    intel_type = intel_type.upper().strip()
    if intel_type not in INTEL_TYPES:
        await interaction.followup.send("intel_type must be ALT / NOTE / FLAG", ephemeral=True)
        return

    lookup = must_json(api_get(f"/api/users/lookup?identifier={requests.utils.quote(identifier)}"))
    user = lookup["user"]
    cases = lookup["cases"]

    must_json(api_post(f"/api/users/{user['id']}/intel", {
        "intel_type": intel_type,
        "value": value,
        "author": f"{interaction.user} ({interaction.user.id})"
    }))

    # Mirror to latest case thread if exists
    if cases:
        latest = cases[0]
        if latest.get("thread_id"):
            thread = await get_thread(interaction.guild, int(latest["thread_id"]))
            await post_to_thread_locked(
                thread,
                f"🧠 **User Intel Added** by {interaction.user}\n"
                f"User: `{identifier}`\n"
                f"Type: `{intel_type}`\n"
                f"```{value}```"
            )

    await interaction.followup.send(
        f"✅ Intel added to **{identifier}**.\nWeb: {API_BASE}/user/{user['id']}",
        ephemeral=True
    )


@bot.tree.command(name="user-view", description="Get the dossier link for a user identifier")
@staff_check()
async def user_view(interaction: discord.Interaction, identifier: str):
    await interaction.response.defer(ephemeral=True)
    lookup = must_json(api_get(f"/api/users/lookup?identifier={requests.utils.quote(identifier)}"))
    user = lookup["user"]
    await interaction.followup.send(f"📄 Dossier: {API_BASE}/user/{user['id']}", ephemeral=True)


@bot.tree.command(name="registry-dashboard", description="Get the analytics dashboard link")
@staff_check()
async def registry_dashboard(interaction: discord.Interaction):
    await interaction.response.send_message(f"📊 Dashboard: {API_BASE}/dashboard", ephemeral=True)


bot.run(TOKEN)
