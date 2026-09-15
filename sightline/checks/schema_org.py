"""The schema.org Organization branch, resolved once at build time.

Checks keep needing to answer one question — "is this node an Organization?"
— and an exact string match on "Organization" answers it wrong for every
subtype. A site marked up as NGO, Plumber or Dentist *is* an Organization;
schema.org says so (Thing > Organization > NGO). Matching the literal string
made structured_data report "No Organization node found in JSON-LD on this
page" on 43 of our own scanned domains that plainly had one, while
entity_consistency reported the same node as present on the same scan. See
COPY.md rule 9.

The set is GENERATED, not curated. Hardcoding the handful of types we had
happened to see in scan data would only move the false finding to the next
prospect, so the block below is the whole branch as published. Regenerate
with tools/regen_schema_org.py after a schema.org release — that script is
the only place in the repo that fetches the vocabulary, because a scan must
never depend on schema.org being reachable.

Both structured_data.py and entity_consistency.py read ORGANIZATION_TYPES
from here. Neither keeps its own list; the second list is what caused this.
"""
from __future__ import annotations

from typing import Iterable

# --- BEGIN GENERATED (tools/regen_schema_org.py) ---
# schema.org 30.0, generated 2026-09-15 from
# https://schema.org/version/latest/schemaorg-current-https.jsonld
# 166 classes transitively under schema:Organization.
_GENERATED_SUBTYPES = frozenset({
    "AccountingService", "AdultEntertainment", "Airline", "AmusementPark",
    "AnimalShelter", "ArchiveOrganization", "ArtGallery", "Attorney",
    "AutoBodyShop", "AutoDealer", "AutoPartsStore", "AutoRental",
    "AutoRepair", "AutoWash", "AutomatedTeller", "AutomotiveBusiness",
    "Bakery", "BankOrCreditUnion", "BarOrPub", "BeautySalon",
    "BedAndBreakfast", "BikeStore", "BookStore", "BowlingAlley", "Brewery",
    "CafeOrCoffeeShop", "Campground", "Casino", "ChildCare", "ClothingStore",
    "CollegeOrUniversity", "ComedyClub", "ComputerStore", "Consortium",
    "ConvenienceStore", "Cooperative", "Corporation", "CovidTestingFacility",
    "DanceGroup", "DaySpa", "Dentist", "DepartmentStore", "DiagnosticLab",
    "Distillery", "DryCleaningOrLaundry", "EducationalOrganization",
    "Electrician", "ElectronicsStore", "ElementarySchool", "EmergencyService",
    "EmploymentAgency", "EntertainmentBusiness", "ExerciseGym",
    "FastFoodRestaurant", "FinancialService", "FireStation", "Florist",
    "FoodEstablishment", "FundingAgency", "FundingScheme", "FurnitureStore",
    "GardenStore", "GasStation", "GeneralContractor", "GolfCourse",
    "GovernmentOffice", "GovernmentOrganization", "GroceryStore",
    "HVACBusiness", "HairSalon", "HardwareStore", "HealthAndBeautyBusiness",
    "HealthClub", "HighSchool", "HobbyShop", "HomeAndConstructionBusiness",
    "HomeGoodsStore", "Hospital", "Hostel", "Hotel", "HousePainter",
    "IceCreamShop", "IndividualPhysician", "InsuranceAgency", "InternetCafe",
    "JewelryStore", "LegalService", "Library", "LibrarySystem", "LiquorStore",
    "LocalBusiness", "Locksmith", "LodgingBusiness", "MedicalBusiness",
    "MedicalClinic", "MedicalOrganization", "MensClothingStore",
    "MiddleSchool", "MobilePhoneStore", "Motel", "MotorcycleDealer",
    "MotorcycleRepair", "MovieRentalStore", "MovieTheater", "MovingCompany",
    "MusicGroup", "MusicStore", "NGO", "NailSalon", "NewsMediaOrganization",
    "NightClub", "Notary", "OfficeEquipmentStore", "OnlineBusiness",
    "OnlineMarketplace", "OnlineStore", "Optician", "OutletStore", "PawnShop",
    "PerformingGroup", "PetStore", "Pharmacy", "Physician",
    "PhysiciansOffice", "Plumber", "PoliceStation", "PoliticalParty",
    "PostOffice", "Preschool", "ProfessionalService", "Project",
    "PublicSwimmingPool", "RadioStation", "RealEstateAgent",
    "RecyclingCenter", "ResearchOrganization", "ResearchProject", "Resort",
    "Restaurant", "RoofingContractor", "School", "SearchRescueOrganization",
    "SelfStorage", "ShoeStore", "ShoppingCenter", "SkiResort",
    "SportingGoodsStore", "SportsActivityLocation", "SportsClub",
    "SportsOrganization", "SportsTeam", "StadiumOrArena", "Store",
    "TattooParlor", "TelevisionStation", "TennisComplex", "TheaterGroup",
    "TireShop", "TouristInformationCenter", "ToyStore", "TravelAgency",
    "VacationRental", "VeterinaryCare", "WholesaleStore", "Winery",
    "WorkersUnion",
})
SCHEMA_ORG_VERSION = '30.0'
SCHEMA_ORG_GENERATED = '2026-09-15'
# --- END GENERATED ---

# Types that are NOT in the schema.org vocabulary but appear in real markup,
# so a node carrying one is still an organization for our purposes. Kept
# separate from the generated block so its provenance stays honest and a
# regeneration never silently drops them.
#
# NonprofitOrganization is the common one: it looks like a schema.org type
# and is not one — the vocabulary spells this NGO, and the Nonprofit501c3
# family are NonprofitType enumeration values, not classes. Sites (and a few
# CMS plugins) emit it anyway.
ORGANIZATION_ALIASES = frozenset({
    "NonprofitOrganization",
})

# What a check should test against: the root, its whole published branch, and
# the out-of-vocabulary aliases.
ORGANIZATION_TYPES = (frozenset({"Organization"})
                      | _GENERATED_SUBTYPES
                      | ORGANIZATION_ALIASES)


def is_organization(types: Iterable[str]) -> bool:
    """True if any of `types` is Organization or one of its subtypes."""
    return bool(set(types) & ORGANIZATION_TYPES)


def organization_subtype(types: Iterable[str]) -> str | None:
    """The most specific organization type declared, or None.

    "Most specific" here only means "not the bare root": we deliberately do
    not ship the depth map, because the one caller
    (structured_data._detect_business_type) has already tried its own
    precedence list and just needs a concrete name for the report. Sorting
    is safe for the same reason findings.CATEGORY_WORDS could not sort —
    'Organization' is excluded, so alphabetical order cannot make the
    generic answer beat the specific one.
    """
    subtypes = (set(types) & ORGANIZATION_TYPES) - {"Organization"}
    return sorted(subtypes)[0] if subtypes else None
