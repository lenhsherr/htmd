import numpy as np
from copy import copy
import logging
import re
import os

logger = logging.getLogger(__name__)


_ATOM_TYPE_REG_EX = re.compile('^\S+x\d+$')


def getEquivalentsAndDihedrals(mol):
    from htmd.molecule.util import guessAnglesAndDihedrals
    from htmd.parameterization.detectsoftdihedrals import detectSoftDihedrals
    from htmd.parameterization.detectequivalents import detectEquivalents

    mol = mol.copy()

    # Guess bonds
    if len(mol.bonds) == 0:
        logger.warning('No bonds found! Guessing them...')
        mol.bonds = mol._guessBonds()

    mol.angles, mol.dihedrals = guessAnglesAndDihedrals(mol.bonds, cyclicdih=True)
    equivalents = detectEquivalents(mol)
    all_dihedrals = detectSoftDihedrals(mol, equivalents)
    return mol, equivalents, {'-'.join(mol.name[dihedral.atoms]): dihedral for dihedral in all_dihedrals}


def canonicalizeAtomNames(mol, inplace=False):
    """
    This fixes up the atom naming and reside name to be consistent.
    NB this scheme matches what MATCH does.
    Don't change it or the naming will be inconsistent with the RTF.
    """
    if not inplace:
        mol = mol.copy()
    mol.segid[:] = 'L'
    logger.info('Rename segment to %s' % mol.segid[0])
    mol.resname[:] = 'MOL'
    logger.info('Rename residue to %s' % mol.resname[0])

    sufices = {}
    for i in range(mol.numAtoms):
        name = guessElementFromName(mol.name[i]).upper()

        sufices[name] = sufices.get(name, 0) + 1
        name += str(sufices[name])

        logger.info('Rename atom %d: %-4s --> %-4s' % (i, mol.name[i], name))
        mol.name[i] = name

    if not inplace:
        return mol


def guessElementFromName(name):
    '''
    Guess element from an atom name

    >>> from htmd.parameterization.util import guessElementFromName
    >>> guessElementFromName('C')
    'C'
    >>> guessElementFromName('C1')
    'C'
    >>> guessElementFromName('C42')
    'C'
    >>> guessElementFromName('C7S')
    'C'
    >>> guessElementFromName('HN1')
    'H'
    >>> guessElementFromName('CL')
    'Cl'
    >>> guessElementFromName('CA1')
    'Ca'
    '''
    import periodictable
    symbol = name.capitalize()

    while symbol:
        try:
            element = periodictable.elements.symbol(symbol)
        except ValueError:
            symbol = symbol[:-1]
        else:
            return element.symbol

    raise ValueError('Cannot guess element from atom name: {}'.format(name))


def getImproper(type, parameters):
    from itertools import permutations
    type = np.array(type)
    perms = np.array([x for x in list(permutations((0, 1, 2, 3))) if x[2] == 2])
    for p in perms:
        if tuple(type[p]) in parameters.improper_types:
            return parameters.improper_types[tuple(type[p])], 'improper_types'
        elif tuple(type[p]) in parameters.improper_periodic_types:
            return parameters.improper_periodic_types[tuple(type[p])], 'improper_periodic_types'
    raise RuntimeError('Could not find improper parameters for key {}'.format(type))


def recreateParameters(mol, originaltypes, parameters):
    from copy import deepcopy, copy
    from itertools import permutations
    from parmed.parameters import ParameterSet

    newparams = ParameterSet()
    uqtypes = np.unique(mol.atomtype)

    for type in uqtypes:
        newparams.atom_types[type] = copy(parameters.atom_types[originaltypes[type]])

    for idx in mol.bonds:
        newkey = tuple(mol.atomtype[idx])
        oldkey = tuple(np.vectorize(originaltypes.get)(newkey))
        newparams.bond_types[newkey] = copy(parameters.bond_types[oldkey])

    for idx in mol.angles:
        newkey = tuple(mol.atomtype[idx])
        oldkey = tuple(np.vectorize(originaltypes.get)(newkey))
        newparams.angle_types[newkey] = copy(parameters.angle_types[oldkey])

    for idx in mol.dihedrals:
        newkey = tuple(mol.atomtype[idx])
        oldkey = tuple(np.vectorize(originaltypes.get)(newkey))
        newparams.dihedral_types[newkey] = copy(parameters.dihedral_types[oldkey])

    for idx in mol.impropers:
        newkey = tuple(mol.atomtype[idx])
        oldkey = np.vectorize(originaltypes.get)(newkey)
        oldval, field = getImproper(oldkey, parameters)
        newparams.__dict__[field][newkey] = copy(oldval)

    return newparams



def inventAtomTypes(mol, fit_dihedrals, equivalents):
    """
    Duplicate atom types of the dihedral, so its parameters are unique.
    """
    # TODO check symmetry

    mol = mol.copy()

    alltypes = list(mol.atomtype)
    originaltype = {type: type for type in np.unique(mol.atomtype)}

    # Duplicate the atom types of the dihedral
    for dih in fit_dihedrals:
        for d in dih:
            oldtype = mol.atomtype[d]
            # if the type is already duplicated
            if re.match(_ATOM_TYPE_REG_EX, oldtype):
                continue
            # Create a new atom type name
            i = 0
            while ('{}x{}'.format(oldtype, i)) in alltypes:
                i += 1
            newtype = '{}x{}'.format(oldtype, i)
            alltypes.append(newtype)
            originaltype[newtype] = oldtype

            mol.atomtype[d] = newtype
            # Rename the atom types of the equivalent atoms
            for index in equivalents[1][d]:
                if index != d:
                    assert not re.match(_ATOM_TYPE_REG_EX, mol.atomtype[index])
                    mol.atomtype[index] = newtype

        equivalent_dihedrals = _getEquivalentDihedrals(mol, equivalents, np.array(dih))
        if len(equivalent_dihedrals) > 1:
            print(dih)
            print(len(equivalent_dihedrals))
            print(equivalent_dihedrals)
            raise ValueError("Dihedral term still not unique after duplication")

    return mol, originaltype


def _duplicateParameters(oldtype, newtype, prm, fields=('bond_types', 'angle_types', 'dihedral_types', 'improper_types', 'improper_periodic_types')):
    for f in fields:
        toadd = []
        for key in prm.__dict__[f]:
            if oldtype in key:
                newkey = np.array(key, dtype=object)
                newkey[newkey == oldtype] = newtype
                toadd.append([tuple(newkey), copy(prm.__dict__[f][key])])

        for ta in toadd:
            prm.__dict__[f][ta[0]] = ta[1]


def _duplicateAtomType(mol, prm, equivalents, atom_index):
    """Duplicate the type of the specified atom

       Duplicated types are named: original_name + "x" + number, e.g. ca --> cax0
    """
    _equivalent_atoms = equivalents[1]

    # Get a type name
    oldtype = mol.atomtype[atom_index]

    # if the type is already duplicated
    if re.match(_ATOM_TYPE_REG_EX, oldtype):
        return

    # Create a new atom type name
    i = 0
    while ('{}x{}'.format(oldtype, i)) in prm.atom_types:
        i += 1
    newtype = '{}x{}'.format(oldtype, i)
    logger.info('Create a new atom type %s from %s' % (newtype, oldtype))

    mol.atomtype[atom_index] = newtype

    # Duplicate the atom type
    prm.atom_types[newtype] = copy(prm.atom_types[oldtype])

    # Duplicate all the parameters
    _duplicateParameters(oldtype, newtype, prm)

    # Rename the atom types of the equivalent atoms
    for index in _equivalent_atoms[atom_index]:
        if atom_index != index:
            assert not re.match(_ATOM_TYPE_REG_EX, mol.atomtype[index])
            mol.atomtype[index] = newtype


def inventNewDihedralTypes(mol, prm, equivalents, dihedral):
    """
    Duplicate atom types of the dihedral, so its parameters are unique.
    """
    # TODO check symmetry

    # Duplicate the atom types of the dihedral
    for i in range(4):
        _duplicateAtomType(mol, prm, equivalents, dihedral[i])

    equivalent_dihedrals = _getEquivalentDihedrals(mol, equivalents, np.array(dihedral))
    if len(equivalent_dihedrals) > 1:
        print(dihedral)
        print(len(equivalent_dihedrals))
        print(equivalent_dihedrals)
        raise ValueError("Dihedral term still not unique after duplication")


def _getEquivalentDihedrals(mol, equivalents, dihedral):
    """
    Find equivalent dihedral angles to the specificied one.
    """
    equivalent_group_by_atom = equivalents[2]
    types = mol.atomtype[dihedral]

    same_type_dihedrals = []
    for dihedral_indices in mol.dihedrals:
        dihedral_types = mol.atomtype[dihedral_indices]
        if np.all(types == dihedral_types) or np.all(types == dihedral_types[::-1]):
            same_type_dihedrals.append(dihedral_indices)

    # Now for each of the uses, remove any which are equivalent
    unique_dihedrals = [dihedral]
    groups = [equivalent_group_by_atom[index] for index in dihedral]
    for dihed in same_type_dihedrals:
        dihedral_groups = [equivalent_group_by_atom[index] for index in dihed]
        if groups != dihedral_groups and groups != dihedral_groups[::-1]:
            unique_dihedrals.append(dihed)

    return unique_dihedrals


def _qm_method_name(qm):
    basis = qm.basis
    basis = re.sub('\+', 'plus', basis)  # Replace '+' with 'plus'
    basis = re.sub('\*', 'star', basis)  # Replace '*' with 'star'
    name = qm.theory + '-' + basis + '-' + qm.solvent
    return name


def getFixedChargeAtomIndices(mol, fix_charge):
    fixed_atom_indices = []
    for fixed_atom_name in fix_charge:

        if fixed_atom_name not in mol.name:
            raise ValueError('Atom {} is not found. Check --fix-charge arguments'.format(fixed_atom_name))

        for aton_index in range(mol.numAtoms):
            if mol.name[aton_index] == fixed_atom_name:
                fixed_atom_indices.append(aton_index)
                logger.info('Charge of atom {} is fixed to {}'.format(fixed_atom_name, mol.charge[aton_index]))
    return fixed_atom_indices


def minimize(mol, qm, outdir):
    assert mol.numFrames == 1

    mindir = os.path.join(outdir, "minimize", _qm_method_name(qm))
    os.makedirs(mindir, exist_ok=True)

    qm.molecule = mol
    qm.esp_points = None
    qm.optimize = True
    qm.restrained_dihedrals = None
    qm.directory = mindir
    results = qm.run()
    if results[0].errored:
        raise RuntimeError('\nQM minimization failed! Check logs at %s\n' % mindir)

    mol = mol.copy()
    # Replace coordinates with the minimized set
    mol.coords = np.atleast_3d(np.array(results[0].coords, dtype=np.float32))
    return mol


def fitCharges(mol, qm, equivalents, netcharge, outdir, fixed=()):
    from htmd.parameterization.esp import ESP

    # Create an ESP directory
    espDir = os.path.join(outdir, "esp", _qm_method_name(qm))
    os.makedirs(espDir, exist_ok=True)

    # Get ESP points
    point_file = os.path.join(espDir, "00000", "grid.dat")
    if os.path.exists(point_file):
        # Load a point file if one exists from a previous job
        esp_points = np.loadtxt(point_file)
        logger.info('Reusing ESP grid from %s' % point_file)
    else:
        # Generate ESP points
        esp_points = ESP.generate_points(mol)[0]

    # Run QM simulation
    qm.molecule = mol
    qm.esp_points = esp_points
    qm.optimize = False
    qm.restrained_dihedrals = None
    qm.directory = espDir
    qm_results = qm.run()
    if qm_results[0].errored:
        raise RuntimeError('\nQM calculation failed! Check logs at %s\n' % espDir)

    # Safeguard QM code from changing coordinates :)
    assert np.all(np.isclose(mol.coords, qm_results[0].coords, atol=1e-6))

    # Fit ESP charges
    esp = ESP()
    esp.molecule = mol
    esp.qm_results = qm_results
    esp.fixed = fixed
    esp._equivalent_atom_groups = equivalents[0]
    esp._equivalent_group_by_atom = equivalents[2]
    esp._netcharge = netcharge
    esp_result = esp.run()
    esp_charges, esp_loss = esp_result['charges'], esp_result['loss']

    # Update the charges
    mol = mol.copy()
    mol.charge[:] = esp_charges
    # self._rtf.updateCharges(esp_charges)
    for name, charge in zip(mol.name, mol.charge):
        logger.info('Set charge {}: {:6.3f}'.format(name, charge))

    return mol, esp_loss, esp_charges, qm_results[0].dipole


def fitDihedrals(mol, qm, method, prm, all_dihedrals, dihedrals, outdir, geomopt=True):
    """
    Dihedrals passed as 4 atom indices
    """
    from htmd.parameterization.dihedral2 import DihedralFitting2
    from htmd.qm import FakeQM2

    # Create molecules with rotamers
    molecules = []
    for dihedral in dihedrals:
        nrotamers = 36  # Number of rotamers for each dihedral to compute

        # Create a copy of molecule with "nrotamers" frames
        tmpmol = mol.copy()
        tmpmol.coords = np.tile(tmpmol.coords, (1, 1, nrotamers))

        # Set rotamer coordinates
        angles = np.linspace(-np.pi, np.pi, num=nrotamers, endpoint=False)
        for frame, angle in enumerate(angles):
            tmpmol.frame = frame
            tmpmol.setDihedral(dihedral, angle, bonds=tmpmol.bonds)

        molecules.append(tmpmol)

    # Create directories for QM data
    directories = []
    dihedral_directory = 'dihedral-opt' if geomopt else 'dihedral-single-point'
    for dihedral in dihedrals:
        dihedral_name = '-'.join(mol.name[dihedral])
        directory = os.path.join(outdir, dihedral_directory, dihedral_name, _qm_method_name(qm))
        os.makedirs(directory, exist_ok=True)
        directories.append(directory)

    # Setup and submit QM calculations
    for molecule, dihedral, directory in zip(molecules, dihedrals, directories):
        qm.molecule = molecule
        qm.esp_points = None
        qm.optimize = geomopt
        qm.restrained_dihedrals = np.array([dihedral])
        qm.directory = directory
        qm.setup()
        qm.submit()

    # Wait and retrieve QM calculation data
    qm_results = []
    for molecule, dihedral, directory in zip(molecules, dihedrals, directories):
        qm.molecule = molecule
        qm.esp_points = None
        qm.optimize = geomopt
        qm.restrained_dihedrals = np.array([dihedral])
        qm.directory = directory
        qm.setup() # QM object is reused, so it has to be properly set up before retrieving.
        qm_results.append(qm.retrieve())

    # Fit the dihedral parameters
    df = DihedralFitting2()
    df.parmedMode = True
    df.parameters = prm
    df._rotatable_dihedrals = [all_dihedrals[key] for key in all_dihedrals]
    df.molecule = mol
    df.dihedrals = dihedrals
    df.qm_results = qm_results
    df.result_directory = os.path.join(outdir, 'parameters', method.name, _qm_method_name(qm), 'plots')

    # In case of FakeQM, the initial parameters are set to zeros.
    # It prevents DihedralFitting class from cheating :D
    if isinstance(qm, FakeQM2):
        df.zeroed_parameters = True

    # Fit dihedral parameters
    df.run()

    return prm  # TODO: Don't allow it to modify prm in place. Or make a copy before

    # # Update atom types
    # self.atomtype[:] = [self._rtf.type_by_name[name] for name in self.name]

def updateDihedral(prm, newparams):
    for p in newparams:
        prm.dihedral_types[p.types].phi_k = p.k0
        prm.dihedral_types[p.types].phase = p.phi0

def createMultitermDihedralTypes(parameters, nterms=6, scee=1.2, scnb=2):
    from parmed.topologyobjects import DihedralTypeList, DihedralType
    from copy import deepcopy

    parameters = deepcopy(parameters)

    for key, val in parameters.dihedral_types.items():
        dihlist = DihedralTypeList()
        for i in range(1, nterms+1):
            found = False
            for d in val: # Check if this term already exists in the parameters.
                if d.per == i:
                    dihlist.append(d)
                    found = True
                    break
            if not found: # Else create an unparametrized term
                dihtype = DihedralType(0, i, 0, scee=1.2, scnb=2)
                dihlist.append(dihtype)
        parameters.dihedral_types[key] = dihlist

    return parameters


def centreOfMass(mol):
    return np.dot(mol.masses, mol.coords[:, :, mol.frame]) / np.sum(mol.masses)


def getDipole(mol):
    """Calculate the dipole moment (in Debyes) of the molecule"""
    from scipy import constants as const

    coords = mol.coords[:, :, mol.frame] - centreOfMass(mol)

    dipole = np.zeros(4)
    dipole[:3] = np.dot(mol.charge, coords)
    dipole[3] = np.linalg.norm(dipole[:3]) # Total dipole moment
    dipole *= 1e11*const.elementary_charge*const.speed_of_light # e * Ang --> Debye (https://en.wikipedia.org/wiki/Debye)

    return dipole

